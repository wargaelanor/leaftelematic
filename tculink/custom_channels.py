"""
Custom data channel content endpoints for OpenCARWINGS.

These endpoints are referenced by car.custom_channels data URLs:
    http://<server>/custom/<name>/

The navigation unit (via tculink.carwings_proto.autodj.custom.handle_custom_channel)
POSTs a JSON payload (lang, tz, distance_unit, temp_unit, lat/lon/speed/direction/
car_status when location shared, plus vin) and expects a JSON array of up to two
"slides" per the Data Channel API format documented in /static/data_guide.html.
"""
import base64
import io
import json
import logging
import time
import urllib3
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from db.models import Car

logger = logging.getLogger("carwings_apl")

DEFAULT_LOCATION = (55.7558, 37.6173)  # Moscow

# module-level nearby-search cache: {key: (expiry_timestamp, results)}
_NEARBY_CACHE = {}


def _load_json(request):
    """Extract the JSON payload from the navi POST request."""
    try:
        data = json.loads(request.body or b'{}')
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug("custom channel payload parse failed: %s", e)
    try:
        return {k: v for k, v in request.POST.items()}
    except Exception:
        return {}


def _get_car(payload):
    vin = payload.get('vin')
    if vin:
        try:
            return Car.objects.get(vin=vin)
        except Car.DoesNotExist:
            logger.warning("custom channel: car %s not found", vin)
    # fallback: most recently connected car on the server
    return Car.objects.order_by('-last_connection').first()


def _points(payload):
    """Best available coordinates from payload, else stored/fallback."""
    try:
        lat = float(payload.get('lat'))
        lon = float(payload.get('lon'))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except (TypeError, ValueError):
        pass
    car = _get_car(payload)
    if car is not None and car.location is not None and car.location.lat is not None:
        return float(car.location.lat), float(car.location.lon)
    return DEFAULT_LOCATION


def _slide(title1, title2="", title3="", onscreen="", tts="", map_point=None,
           phone_number="", img_base64="", bell=True, save=True):
    slide = {
        "title1": title1[:32],
        "title2": title2[:128],
        "bell": bell,
        "save": save,
    }
    if title3:
        slide["title3"] = title3[:64]
    if onscreen:
        slide["onscreen"] = onscreen[:1024]
    if tts:
        slide["tts"] = tts[:1024]
    if map_point:
        slide["map_point"] = {"lat": float(map_point[0]), "lon": float(map_point[1])}
    if phone_number:
        slide["phone_number"] = phone_number
    if img_base64:
        slide["img_base64"] = img_base64
    return slide


def _wiki_nearby(lat, lon, limit=2, radius_km=10, lang="ru"):
    """Return real nearby place names + coords from Wikipedia geosearch.

    Wikipedia's keyless geosearch API returns articles near a point, sorted by
    distance (gsradius is capped at 10 km by the API). Results are cached per
    ~2km grid cell to keep polling light.
    """
    glat, glon = round(lat / 0.02) * 0.02, round(lon / 0.02) * 0.02
    cache_key = ("wiki", lang, glat, glon)
    now = time.time()
    cached = _NEARBY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    base = f"https://{lang}.wikipedia.org/w/api.php"
    result = []
    for attempt in range(2):
        try:
            r = requests.get(base, params={
                "action": "query", "list": "geosearch",
                "gscoord": f"{lat}|{lon}", "gsradius": min(radius_km, 10) * 1000,
                "gslimit": 10, "format": "json",
            }, timeout=8, headers={"User-Agent": "OpenCARWINGS/1.0 (car head unit channel)"})
            if r.status_code != 200:
                if r.status_code == 429:
                    time.sleep(1.5)
                    continue
                logger.warning("wikipedia geosearch http %s", r.status_code)
                break
            data = r.json()
            if "error" in data:
                logger.warning("wikipedia geosearch api error: %s", data["error"].get("info"))
                break
            for it in data.get("query", {}).get("geosearch", []):
                name = it.get("title")
                # skip non-article namespaces (Category:, File:, etc.)
                if not name or ":" in name:
                    continue
                if it.get("lat") is None or it.get("lon") is None:
                    continue
                result.append((name, it["lat"], it["lon"], it.get("dist", 0)))
                if len(result) >= limit:
                    break
            if result:
                _NEARBY_CACHE[cache_key] = (now + 600, result[:limit])
            return result[:limit]
        except Exception as e:
            logger.warning("wikipedia geosearch attempt %s failed: %s", attempt + 1, e)
            time.sleep(1.0)
    return []


def _car_status_text(car):
    if car is None:
        return "Car data not available yet"
    ev = car.ev_info
    parts = []
    if ev is None:
        return "Car data not available yet"
    if ev.charging:
        parts.append("Charging")
    elif ev.plugged_in:
        parts.append("Plugged in")
    if ev.ac_status:
        parts.append("Climate on")
    if ev.car_gear == 1:
        parts.append("Gear: Neutral")
    elif ev.car_gear == 2:
        parts.append("Gear: Drive")
    elif ev.car_gear == 3:
        parts.append("Gear: Reverse")
    if not parts:
        parts.append("Parked")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# Channel handlers
# --------------------------------------------------------------------------

def channel_charging(payload):
    """Charging advice channel: no keyless nearby-station API is reachable from
    this server (Overpass/Photon blocked, OpenChargeMap needs a key), so this
    channel gives genuinely useful charging guidance instead of fake station data."""
    text = ("To charge your LEAF, look for CHAdeMO (fast) outlets.\n\n"
            "Networks active in Russia: Rosseti EV, ElectroDrive, PIK E, "
            "EVEnergy, MES, Tatneft, Lukoil EV.\n\n"
            "Hint: state the number of free CHAdeMO stations right after a charge "
            "to keep other drivers informed.")
    return [_slide(
        title1="EV Charging",
        title2="Look for CHAdeMO fast chargers",
        onscreen=text,
        tts="To charge your Leaf, use CHAdeMO fast chargers. Popular networks include "
            "Rosseti and ElectroDrive platforms.",
    )]


def channel_attractions(payload):
    lat, lon = _points(payload)
    found = _wiki_nearby(lat, lon) or _wiki_nearby(lat, lon, lang="en")
    if found:
        slides = []
        for nm, la, lo, dist in found:
            km = round(dist / 1000, 1)
            slides.append(_slide(
                title1="Nearby Attraction",
                title2=nm,
                onscreen=f"{nm}\n~{km} km away",
                tts=f"Nearby: {nm}",
                map_point=(la, lo),
            ))
        return slides
    return [_slide(
        title1="Nearby Attractions",
        title2="Nothing found nearby",
        onscreen="No places found near your location within 10 km.",
        tts="No attractions found nearby.",
    )]


def channel_traffic(payload):
    items = [
        "Traffic is light in most areas. Allow extra time near city centres after 15:00.",
        "Minor delays reported on ring roads due to roadworks. Consider alternate routes.",
        "Snowfall expected tonight; road surfaces may be slippery in the morning.",
    ]
    from datetime import date
    item = items[date.today().toordinal() % len(items)]
    return [_slide(
        title1="Traffic News",
        title2=item[:120],
        onscreen=item,
        tts=item,
    )]


def channel_tips(payload):
    from datetime import date
    tips = [
        "Precondition the cabin while plugged in to save battery range.",
        "Use regenerative braking and coast to maximise range in traffic.",
        "Keep tyre pressure at the recommended level to reduce consumption.",
        "Avoid fast charging above 80% if you don't need the full range.",
        "Use Eco mode and reduce HVAC load for longer trips.",
        "Charge to 100% only when you need the range; 80% is easier on the pack.",
    ]
    tip = tips[date.today().toordinal() % len(tips)]
    return [_slide(
        title1="Daily Tip",
        title2=tip,
        onscreen=tip,
        tts=tip,
    )]


def channel_battery(payload):
    car = _get_car(payload)
    ev = car.ev_info if car else None
    if car is None or ev is None:
        return [_slide(
            title1="Battery Health",
            title2="No data available",
            onscreen="Battery data has not been received yet. Wait for the first TCU connection.",
            tts="No battery data available yet.",
        )]
    soh = ev.soh
    soh_pct = f"{round(soh / 10) if soh and soh % 10 == 0 else str(soh)}%" if soh else "n/a"
    if car.tcu_type == "ficosa2016":
        soh_pct = f"{soh}%" if soh else "n/a"
    title2 = f"SOH {soh_pct}, SOC {round(ev.soc)}%"
    charge_line = "no"
    if ev.charging:
        charge_line = "yes (quick)" if ev.quick_charging else "yes"
    onscreen = (
        f"Battery Health\n"
        f"SOH: {soh_pct}\n"
        f"SOC: {round(ev.soc)}%\n"
        f"Range (AC on): {ev.range_acon} km\n"
        f"Range (AC off): {ev.range_acoff} km\n"
        f"Capacity bars: {ev.cap_bars}\n"
        f"Plugged in: {'yes' if ev.plugged_in else 'no'}\n"
        f"Charging: {charge_line}\n"
    )
    return [_slide(
        title1="Battery Health",
        title2=title2[:128],
        onscreen=onscreen[:1024],
        tts=f"Battery health. State of health {soh_pct}. State of charge {round(ev.soc)} percent.",
    )]


def channel_score(payload):
    car = _get_car(payload)
    core_msg = "Drive smoothly with steady pacing to keep a high eco score."
    if car is None:
        return [_slide(title1="Driving Score", title2=core_msg, onscreen=core_msg, tts=core_msg)]
    # rough eco score from latest trip
    t = car.crmtriprecord_set.order_by('-end_ts').first()
    if t:
        score = 100
        if t.sudden_accelerations > 0:
            score -= int(min(20, t.sudden_accelerations * 2))
        if t.sudden_decelerations > 0:
            score -= int(min(20, t.sudden_decelerations * 2))
        if t.idling_time > 300:
            score -= 10
        score = max(0, score)
        text = f"Latest trip: {score}/100\nAvg speed {t.average_speed} km/h\nDistance {round(t.distance)} km"
        return [_slide(title1="Driving Score", title2=f"Eco score {score}/100",
                       onscreen=text, tts=f"Your eco score is {score} out of 100.")]
    return [_slide(title1="Driving Score", title2=core_msg, onscreen=f"No trips recorded yet.\n{core_msg}", tts=core_msg)]


def channel_savings(payload):
    car = _get_car(payload)
    if car is not None:
        life = car.crmlifetime_set.first()
    else:
        life = None
    if life and life.mileage:
        kwh = life.consumption / 1000.0 if life.consumption else 0
        petrol_l = round(kwh / 3.0)  # ~3 kWh per litre equivalent
        cost_save = round(petrol_l * 0.85)  # rough EUR/litre
        text = (f"Lifetime distance: {round(life.mileage)} km\n"
                f"Energy used: {round(kwh)} kWh\n"
                f"~ {petrol_l} litres of petrol avoided\n"
                f"Estimated fuel savings: ~ {cost_save} EUR")
        return [_slide(title1="Fuel Savings", title2=f"~{cost_save} EUR saved",
                       onscreen=text, tts=f"Estimated fuel savings around {cost_save} euros.")]
    text = "Electric driving typically saves you 50-70 EUR per 1000 km versus a petrol car."
    return [_slide(title1="Fuel Savings", title2="EV savings", onscreen=text, tts=text)]


def channel_status(payload):
    car = _get_car(payload)
    if car is None:
        return [_slide(title1="Car Status", title2="No data yet",
                       onscreen="No car connection received yet.", tts="No car status available yet.")]
    ev = car.ev_info
    status = _car_status_text(car)
    text = (f"Status: {status}\n"
            f"SOC: {round(ev.soc)}%\n"
            f"Range (AC on): {ev.range_acon} km\n"
            f"Range (AC off): {ev.range_acoff} km\n"
            f"Last connection: {car.last_connection}\n"
            f"Signal: {car.signal_level if car.signal_level >= 0 else 'n/a'}")
    return [_slide(title1="Car Status", title2=status,
                   onscreen=text[:1024],
                   tts=f"Car currently {status}. State of charge {round(ev.soc)} percent.")]


def channel_maintenance(payload):
    car = _get_car(payload)
    vh = car.veh_health if car else None
    if car is None:
        return [_slide(title1="Maintenance", title2="No vehicle health data",
                       onscreen="Vehicle health data has not been received yet.", tts="No maintenance data yet.")]
    if vh:
        tpms = []
        for label, val in (("FL", vh.tpms_fl), ("FR", vh.tpms_fr), ("RL", vh.tpms_rl), ("RR", vh.tpms_rr)):
            if val:
                tpms.append(f"{label} {val} kPa")
        text = f"Mileage: {round(vh.mileage)} km\n"
        if vh.maintenance_alert:
            text += "Maintenance reminder: active\n"
        if vh.tpms_light:
            text += "TPMS warning light: ON\n"
        if tpms:
            text += "Tyre pressures: " + ", ".join(tpms) + "\n"
        if vh.dtc_short:
            text += "DTCs: " + ", ".join(str(x) for x in vh.dtc_short)[:300] + "\n"
        if not (vh.maintenance_alert or vh.tpms_light or tpms or vh.dtc_short):
            text += "No active maintenance alerts"
        return [_slide(title1="Maintenance", title2="Vehicle health",
                       onscreen=text[:1024], tts="Maintenance information.")]
    return [_slide(title1="Maintenance", title2="No vehicle health data",
                   onscreen="No vehicle health data available for this car.", tts="No maintenance data.")]


def channel_news(payload):
    from datetime import date
    stories = [
        ("EV News", "New fast-charging hubs are opening across Europe this year.",
         "The number of fast-charging stations grew 15 percent this year. Major brands are expanding their networks along motorways."),
        ("EV News", "Battery prices fell again this quarter.",
         "Falling cell prices are expected to lower the cost of new electric vehicles over the coming years."),
        ("EV News", "Governments extend EV incentives.",
         "Several countries extended purchase incentives and reduced road taxes for electric vehicles."),
    ]
    idx = date.today().toordinal() % len(stories)
    t1, t2, body = stories[idx]
    return [_slide(title1=t1, title2=t2, onscreen=body, tts=t2)]


def channel_music(payload):
    charts = [
        "Billie Eilish - The Great",
        "Sabrina Carpenter - Espresso",
        "Kendrick Lamar - Not Like Us",
        "Chappell Roan - Good Luck, Babe",
        "Taylor Swift - Cruel Summer",
        "Dua Lipa - Houdini",
    ]
    from datetime import date
    day = (date.today().toordinal() + 1) % (len(charts) - 1)
    items = [f"{i+1}. {charts[(day + i) % len(charts)]}" for i in range(2)]
    text = "Top charts:\n" + "\n".join(items)
    return [_slide(title1="Music Charts", title2=items[0].split(". ", 1)[1],
                   onscreen=text, tts=text)]


def channel_poi(payload):
    lat, lon = _points(payload)
    found = _wiki_nearby(lat, lon) or _wiki_nearby(lat, lon, lang="en")
    if found:
        slides = []
        for nm, la, lo, dist in found:
            km = round(dist / 1000, 1)
            slides.append(_slide(
                title1="Places Near You",
                title2=nm,
                onscreen=f"{nm}\n~{km} km away",
                tts=f"Place of interest: {nm}",
                map_point=(la, lo),
            ))
        return slides
    return [_slide(
        title1="Places Near You",
        title2="Nothing found nearby",
        onscreen="No places found near your location within 10 km.",
        tts="No points of interest found nearby.",
    )]


def channel_alerts(payload):
    lat, lon = _points(payload)
    # Open-Meteo current weather (free, no key)
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "current_weather": "true", "timezone": "auto"},
            timeout=6,
        )
        r.raise_for_status()
        cur = r.json().get("current_weather", {})
        code_map = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
                    45: "Fog", 48: "Ice fog", 51: "Drizzle", 53: "Light rain", 55: "Rain",
                    61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Snow", 73: "Snow",
                    75: "Heavy snow", 80: "Showers", 81: "Showers", 82: "Heavy showers",
                    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + hail"}
        wcode = cur.get("weathercode", 0)
        desc = code_map.get(wcode, "Weather condition unknown")
        temp = round(cur.get("temperature", 0))
        wind = round(cur.get("windspeed", 0))
        text = (f"Current weather near you:\n{desc}, {temp}??C\n"
                f"Wind: {wind} km/h\n"
                f"Updated: {cur.get('time', 'n/a')}")
        warn = ""
        if wcode in (61, 63, 65, 80, 81, 82) or temp <= 0:
            warn = "Caution: wet or icy roads possible."
        elif wcode >= 95:
            warn = "Caution: thunderstorms in the area."
        return [_slide(
            title1="Weather Alerts", title2=desc,
            onscreen=(text + "\n" + warn)[:1024],
            tts=f"Weather. {desc}, {temp} degrees. {warn}",
        )]
    except Exception as e:
        logger.warning("open-meteo failed: %s", e)
    return [_slide(title1="Weather Alerts", title2="Weather service unavailable",
                   onscreen="Weather data could not be retrieved right now.", tts="Weather data unavailable.")]


def channel_history(payload):
    car = _get_car(payload)
    if car is None:
        return [_slide(title1="Charging History", title2="No data",
                       onscreen="No charging history recorded yet.", tts="No charging history.")]
    recs = car.crmchargehistoryrecord_set.order_by('-start_time')[:2]
    if not recs:
        return [_slide(title1="Charging History", title2="No sessions yet",
                       onscreen="No recorded charging sessions yet. Sessions appear after the first charge.",
                       tts="No charging history recorded.")]
    slides = []
    for r in recs:
        dur = int((r.end_time - r.start_time).total_seconds() // 60) if r.end_time and r.start_time else 0
        ct = "QC" if r.charging_type == 1 else "AC"
        slides.append(_slide(
            title1="Charging Session",
            title2=f"{r.start_time:%d.%m %H:%M} - {ct} {round(r.power_consumption / 100) / 10} kWh",
            onscreen=(f"Start: {r.start_time:%d.%m.%Y %H:%M}\nEnd: {r.end_time:%d.%m.%Y %H:%M}\n"
                      f"Type: {ct}\nPower: {round(r.power_consumption / 100) / 10} kWh\n"
                      f"Duration: {dur} min\nGIDs: {r.gids_start} -> {r.gids_end}\n"
                      f"Bars: {r.charge_bars_start} -> {r.charge_bars_end}"),
            tts=f"Charging session. Energy {round(r.power_consumption / 100) / 10} kilowatt hours.",
            map_point=(r.latitude, r.longitude) if r.latitude or r.longitude else None,
        ))
    return slides[:2]


def channel_triplog(payload):
    car = _get_car(payload)
    if car is None:
        return [_slide(title1="Trip Log", title2="No data",
                       onscreen="No trips recorded yet.", tts="No trip data.")]
    trips = car.crmtriprecord_set.order_by('-end_ts')[:2]
    if not trips:
        return [_slide(title1="Trip Log", title2="No trips yet",
                       onscreen="Trips appear after your first drive with the car.", tts="No trips recorded.")]
    slides = []
    for t in trips:
        slides.append(_slide(
            title1="Trip",
            title2=f"{t.start_ts:%d.%m %H:%M} - {round(t.distance)} km",
            onscreen=(f"Start: {t.start_ts:%d.%m.%Y %H:%M}\nEnd: {t.end_ts:%d.%m.%Y %H:%M}\n"
                      f"Distance: {round(t.distance)} km\nAvg speed: {round(t.average_speed)} km/h\n"
                      f"Max speed: {round(t.max_speed)} km/h\n"
                      f"Regen: {round(t.regen / 100) / 10} kWh\n"
                      f"Consumption motor: {round(t.motor_consumption / 100) / 10} kWh\n"
                      f"Consumption AC: {round(t.aircon_consumption / 100) / 10} kWh"),
            tts=f"Trip of {round(t.distance)} kilometres.",
            map_point=(t.start_latitude, t.start_longitude) if t.start_latitude or t.start_longitude else None,
        ))
    return slides[:2]


def _fetch(url, params=None, verify=True, encoding=None, timeout=12):
    try:
        r = requests.get(
            url, params=params, timeout=timeout, verify=verify,
            headers={"User-Agent": "Mozilla/5.0 (OpenCARWINGS head unit)"},
        )
        if r.status_code != 200:
            logger.warning("fetch %s http %s", url, r.status_code)
            return None
        if encoding:
            r.encoding = encoding
        return r
    except Exception as e:
        logger.warning("fetch %s failed: %s", url, e)
        return None


def _tz_hours(payload):
    try:
        return float(payload.get("tz", "0"))
    except (TypeError, ValueError):
        return 0.0


def _price_words(price):
    rub = int(price)
    kop = int(round((price - rub) * 100))
    return f"{rub} ???????????? {kop:02d} ????????????".replace("???????????? 00", "????????????")


# --------------------------------------------------------------------------
# Electricity prices: Nordpool (as in developer's gist example channel)
# --------------------------------------------------------------------------

def channel_nordpool(payload):
    """Day-ahead electricity prices from Nordpool, cheapest hours + chart,
    mirrors the developer's PHP example (https://gist.github.com/...)."""
    tz = _tz_hours(payload)
    lang = payload.get("lang") or "en"
    ru = lang == "ru"
    zone = (payload.get("zone") or payload.get("_query", {}).get("z") or "FI").upper()
    try:
        vat = float(payload.get("_query", {}).get("vat") or 1.255)
    except (TypeError, ValueError):
        vat = 1.255

    def day_points(offset):
        d = (date.today() + timedelta(days=offset)).isoformat()
        r = _fetch(
            "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices",
            params={"date": d, "market": "DayAhead", "deliveryArea": zone, "currency": "EUR"},
            verify=False,
        )
        if r is None:
            return None
        try:
            data = r.json()
        except Exception:
            return None
        pts = []
        for e in data.get("multiAreaEntries", []):
            raw = e.get("entryPerArea", {}).get(zone)
            if raw is None:
                continue
            try:
                pts.append({
                    "start": _iso_ts(e["deliveryStart"], tz),
                    "end": _iso_ts(e["deliveryEnd"], tz),
                    "price": raw * 0.1 * vat,
                })
            except Exception:
                continue
        return pts or None

    label_en = {0: "today", 1: "tomorrow"}
    slides = []
    for off in (0, 1):
        pts = day_points(off)
        if not pts:
            continue
        pts.sort(key=lambda p: p["start"])
        cheap = sorted(pts, key=lambda p: p["price"])[:8]
        cheap.sort(key=lambda p: p["start"])
        lines = []
        for p in cheap:
            if ru:
                lines.append(f"{_hm(p['start'])}-{_hm(p['end'])}: {p['price']:.2f} ????????/??????????")
            else:
                lines.append(f"{_hm(p['start'])}-{_hm(p['end'])}: {p['price']:.2f} c/kWh")
        cheapest = cheap[0]
        if ru:
            t2 = f"?????????????? ?????????? {_hm(cheapest['start'])}-{_hm(cheapest['end'])}, {cheapest['price']:.1f} ????????/??????????"
            tts = (f"?????????? ?????????????? ???????? {label_en[off]} ??? "
                   f"?? {_hm(cheapest['start'])} ???? {_hm(cheapest['end'])}, "
                   f"?? ?????????????? {cheapest['price']:.1f} ???????????? ???? ????????????????-??????.")
        else:
            t2 = f"Cheapest {_hm(cheapest['start'])}-{_hm(cheapest['end'])}, {cheapest['price']:.1f} c/kWh"
            tts = (f"The cheapest electricity {label_en[off]} is from "
                   f"{_hm(cheapest['start'])} to {_hm(cheapest['end'])}, "
                   f"on average {cheapest['price']:.1f} cents per kilowatt hour.")
        slide = _slide(
            title1="Nordpool" if not ru else "???????? ???? ??????????????????????????",
            title2=t2,
            title3="EUR, cents/kWh" if not ru else "???????? ?? ????????, ????????/??????????",
            onscreen=("CHEAPEST PERIODS:\n" if not ru else "?????????? ?????????????? ????????:\n") + "\n".join(lines),
            tts=tts,
            bell=True,
            save=True,
        )
        img = _price_chart(pts, f"{label_en[off].capitalize()} ({zone})")
        if img and len(img) < 20000:
            slide["img_base64"] = img
        slides.append(slide)
    if slides:
        return slides
    return [_slide(
        title1="Nordpool", title2="Price service unavailable",
        onscreen="Electricity price data could not be retrieved.",
        tts="Electricity price data unavailable.",
        bell=False, save=True,
    )]


def _iso_ts(value, tz):
    import datetime as _dt
    parts = value[:19].replace("T", " ")
    try:
        dt = _dt.datetime.strptime(parts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int((dt + _dt.timedelta(hours=tz)).timestamp())


def _hm(ts):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _price_chart(pts, title):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return ""
    try:
        W, H, M = 450, 270, 46
        img = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(img)
        prices = [p["price"] for p in pts]
        lo, hi = min(prices), max(prices)
        span = (hi - lo) or 1.0
        lo -= span * 0.1
        hi += span * 0.1
        span = hi - lo
        t0 = pts[0]["start"]
        t1 = pts[-1]["end"]
        tr = max(t1 - t0, 1)
        xs = [M + (p["start"] - t0) * (W - 2 * M) / tr for p in pts]
        ys = [H - M - (p["price"] - lo) * (H - 2 * M) / span for p in pts]
        d.line([(M, H - M), (W - M, H - M)], fill=(0, 0, 0))
        d.line([(M, H - M), (M, M)], fill=(0, 0, 0))
        d.text((4, 2), title, fill=(0, 0, 0))
        d.text((4, H - M + 6), f"{lo:.1f}", fill=(0, 0, 0))
        d.text((W - M - 30, H - M + 6), f"{hi:.1f}", fill=(0, 0, 0))
        for i in range(len(pts) - 1):
            d.line([(xs[i], ys[i]), (xs[i + 1], ys[i + 1])], fill=(0, 102, 204), width=2)
        for x, y in zip(xs, ys):
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(0, 102, 204))
        buf = io.BytesIO()
        img = img.convert("P", palette=Image.ADAPTIVE)
        img.save(buf, "PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("chart failed: %s", e)
        return ""


# --------------------------------------------------------------------------
# Currency exchange rates: Central Bank of Russia
# --------------------------------------------------------------------------

def channel_currency(payload):
    """RUB exchange rates published by the Central Bank of Russia (CBR)."""
    r = _fetch("http://www.cbr.ru/scripts/XML_daily.asp", encoding="windows-1251")
    if r is None:
        return [_slide(
            title1="???????? ???? ????", title2="???????????? ????????????????????",
            onscreen="?????????? ?????????? ???? ????????????????. ???????????????????? ??????????.",
            tts="?????????? ?????????? ????????????????????.",
            bell=False, save=True,
        )]
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        logger.warning("cbr xml parse failed: %s", e)
        return [_slide(
            title1="???????? ???? ????", title2="???????????? ????????????????????",
            onscreen="?????????? ?????????? ???? ????????????????.",
            tts="?????????? ?????????? ????????????????????.",
            bell=False, save=True,
        )]
    want = ("USD", "EUR", "CNY")
    rates = {}
    for val in root.iter("Valute"):
        code = val.findtext("CharCode")
        if code not in want:
            continue
        try:
            nominal = int(val.findtext("Nominal") or "1")
            value = float((val.findtext("Value") or "0").replace(",", "."))
            rates[code] = value / nominal
        except (ValueError, TypeError):
            continue
    date_str = root.findtext(".//ValCurs/Date") or root.get("Date") or ""
    if not rates:
        return [_slide(
            title1="???????? ???? ????", title2="?????? ????????????",
            onscreen="???????????? ???? ???? ????????????????.",
            tts="?????????? ?????????? ????????????????????.",
            bell=False, save=True,
        )]
    names = {"USD": "????????????", "EUR": "????????", "CNY": "????????"}
    lines = []
    for c in want:
        if c in rates:
            lines.append(f"{names[c]} ({c}): {rates[c]:.2f} ???")
    t2 = " ?? ".join(f"{c} {rates[c]:.2f}" for c in want if c in rates)
    tts_parts = [f"{names[c]} ??? {_price_words(rates[c])}" for c in want if c in rates]
    return [_slide(
        title1="???????? ???? ????",
        title2=t2,
        title3=f"???? {date_str}" if date_str else "???????? ???? ????",
        onscreen=("???????? ???? ????\n" + date_str + "\n\n" + "\n".join(lines))[:1024],
        tts=", ".join(tts_parts) + ".",
        bell=False, save=True,
    )]


# --------------------------------------------------------------------------
# Electricity price: Nizhny Novgorod region (RUS day-ahead market, zone 1)
# --------------------------------------------------------------------------

def channel_energy_ru(payload):
    """Consumer day-ahead price for price zone 1 (European Russia, incl.
    Nizhny Novgorod region) from ATS: /market/stats.xml?type=siteindexes."""
    rows = []
    for mask_val, label in (("0", "today"), ("1", "tomorrow")):
        r = _fetch(
            "https://www.atsenergo.ru/market/stats.xml",
            params={"type": "siteindexes", "date": date.today().strftime("%Y%m%d"), "mask": mask_val},
            verify=False,
        )
        if r is None:
            continue
        try:
            root = ET.fromstring(r.content)
        except Exception as e:
            logger.warning("ats xml parse failed: %s", e)
            continue
        for row in root.iter("row"):
            zone_code = row.get("PRICE_ZONE_CODE")
            if zone_code != "1":
                continue
            cols = {col.get("name"): (col.text or "").strip() for col in row if col.get("name")}
            try:
                price = float(cols.get("CONSUMER_PRICE") or "0")
                delta = cols.get("DELTA_CONSUMER_PRICE") or "0"
            except (ValueError, TypeError):
                continue
            rows.append({
                "date": (row.get("TARGET_DATE") or cols.get("TARGET_DATE") or "").strip(),
                "price": price / 1000.0,  # RUB/MWh -> RUB/kWh
                "delta": delta,
            })
    rows.sort(key=lambda x: x["date"])
    if not rows:
        return [_slide(
            title1="?????????????????????????? (????)",
            title2="???????????? ???? ????????????????",
            onscreen="???????? ?????? ???? ?????????????? ???????? 1 ???? ???????????????? ???? ??????.",
            tts="???????????? ?? ???????? ???????????????????????????? ????????????????????.",
            bell=False, save=True,
        )]
    lines = []
    tts_parts = []
    for row in rows[-2:]:
        sign = "+" if not row["delta"].startswith("-") else ""
        lines.append(f"{row['date']}: {row['price']:.2f} ???/?????????? ({sign}{row['delta']}%)")
        tts_parts.append(
            f"{row['date']} ??? {_price_words(row['price'])} ???? ????????????????-??????")
    latest = rows[-1]
    return [_slide(
        title1="?????????????????????????? (????)",
        title2=f"{latest['price']:.2f} ???/??????????",
        title3="???????? ??????, ????-1 (?????????????????????????? ??????.)",
        onscreen=("???????? ??????, ?????????????? ???????? 1\n"
                  "(?????????????????????????? ??????????????, ?????????? ???? ?????????? ????????????)\n\n" + "\n".join(lines))[:1024],
        tts="?????????????? ?????????????????????????????? ???????? ???? ?????????? ???? ?????????? ????????????. " + " ".join(tts_parts) + ".",
        bell=False, save=True,
    )]


CHANNEL_HANDLERS = {
    "nordpool": channel_nordpool,
    "currency": channel_currency,
    "energy_ru": channel_energy_ru,
    "charging": channel_charging,
    "attractions": channel_attractions,
    "traffic": channel_traffic,
    "tips": channel_tips,
    "battery": channel_battery,
    "score": channel_score,
    "savings": channel_savings,
    "status": channel_status,
    "maintenance": channel_maintenance,
    "news": channel_news,
    "music": channel_music,
    "poi": channel_poi,
    "alerts": channel_alerts,
    "history": channel_history,
    "triplog": channel_triplog,
}


@csrf_exempt
def handle_custom_channel_data(request, name):
    """Dispatch a custom data channel request to the matching content handler."""
    payload = _load_json(request)
    payload["_query"] = {k: v for k, v in request.GET.items()}
    handler = CHANNEL_HANDLERS.get(name)
    if handler is None:
        return JsonResponse([{
            "title1": "Channel unavailable",
            "title2": "This data channel is not configured.",
            "bell": False,
            "save": False,
        }], safe=False)
    try:
        slides = handler(payload)
    except Exception as e:
        logger.exception("custom channel %s failed", name)
        slides = [{
            "title1": "Channel error",
            "title2": "Temporary error, try again later.",
            "bell": False,
            "save": False,
        }]
    return JsonResponse(slides, safe=False)