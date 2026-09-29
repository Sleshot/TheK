#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_overpass.py — выгрузка OpenStreetMap-данных Краснодара для игры TheK.

Запускается на GitHub Actions раннере (у раннера открыт доступ к Overpass API).
Качает тайлами с ретраями и ротацией зеркал, сохраняет части сразу на диск
(резюмируемо), в конце мёржит и дедуплицирует в merged/*.jsonl.

Данные: © OpenStreetMap contributors, ODbL 1.0.
"""
import json
import os
import sys
import time
import gzip
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

# ---------------- конфиг ----------------
BBOX_S, BBOX_W, BBOX_N, BBOX_E = 44.90, 38.78, 45.17, 39.24

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(REPO_ROOT, "engine", "data_raw")

SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
]

DEADLINE = time.time() + 80 * 60  # глобальный бюджет — 80 минут, дальше фиксируем частичный результат

ROAD_EXCLUDE = "footway|path|cycleway|steps|bridleway|corridor|construction|proposed|elevator|escape|rest_area|services"


def log(*a):
    print(*a, flush=True)


def overpass(query, label, attempts=8):
    """Выполняет запрос к Overpass с ротацией зеркал и экспоненциальным бэкоффом."""
    err = None
    for i in range(attempts):
        if time.time() > DEADLINE:
            raise TimeoutError("global deadline")
        srv = SERVERS[i % len(SERVERS)]
        try:
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(
                srv, data=data,
                headers={"User-Agent": "thek-krasnodar-game/1.0 (github actions)"})
            with urllib.request.urlopen(req, timeout=900) as r:
                body = r.read().decode("utf-8", "replace")
            if "<remark>" in body or '"remark"' in body and 'runtime error' in body:
                raise RuntimeError("overpass remark: " + body[:200])
            if "Error" in body[:400] and "runtime error" in body[:600]:
                raise RuntimeError("overpass error: " + body[:200])
            return body
        except Exception as e:  # noqa
            err = e
            wait = min(60, 4 * (i + 1))
            log(f"  [{label}] attempt {i} failed ({srv}): {e}; sleep {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"query failed after {attempts} attempts: {label}: {err}")


def save_part(group, name, text):
    d = os.path.join(OUT_DIR, "parts", group)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def part_exists(group, name):
    return os.path.exists(os.path.join(OUT_DIR, "parts", group, name))


def grid(step_deg):
    """Генерирует (s,w,n,e) тайлы внутри BBOX."""
    lat = BBOX_S
    while lat < BBOX_N - 1e-9:
        lon = BBOX_W
        n = min(lat + step_deg, BBOX_N)
        while lon < BBOX_E - 1e-9:
            e = min(lon + step_deg, BBOX_E)
            yield (lat, lon, n, e)
            lon += step_deg
        lat += step_deg


def fmt_bbox(t):
    s, w, n, e = t
    return f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"


# ---------------- задачи ----------------
def task_building_tile(t):
    name = f"b_{t[0]:.5f}_{t[1]:.5f}.txt"
    if part_exists("buildings", name):
        return name, True
    bb = fmt_bbox(t)
    # 1) компактный CSV
    q_csv = ('[out:csv(::id,::bbox,"building:levels","building";false;"|")]'
             f'[timeout:900][maxsize:1073741824];way["building"]({bb});out bb;')
    try:
        body = overpass(q_csv, name, attempts=4)
        lines = [l for l in body.splitlines() if "|" in l and l[0].isdigit()]
        if len(lines) < 3:
            raise RuntimeError(f"csv looks empty ({len(lines)} rows)")
        save_part("buildings", name, "\n".join(lines))
        return name, True
    except Exception as e:
        log(f"  [{name}] csv failed: {e}; fallback to json")
    # 2) JSON out bb (полный, чистим на месте: оставляем bounds+tags)
    q_json = f'[out:json][timeout:900][maxsize:1073741824];way["building"]({bb});out bb;'
    body = overpass(q_json, name)
    doc = json.loads(body)
    rows = []
    for el in doc.get("elements", []):
        b = el.get("bounds")
        if not b:
            continue
        tags = el.get("tags") or {}
        lvl = tags.get("building:levels", "")
        typ = tags.get("building", "")
        rows.append(f'{el["id"]}|{b["minlat"]:.7f},{b["minlon"]:.7f},'
                    f'{b["maxlat"]:.7f},{b["maxlon"]:.7f}|{lvl}|{typ}')
    save_part("buildings", name, "\n".join(rows))
    return name, True


def task_road_tile(t):
    name = f"r_{t[0]:.5f}_{t[1]:.5f}.json"
    if part_exists("roads", name):
        return name, True
    bb = fmt_bbox(t)
    q = (f'[out:json][timeout:900][maxsize:2147483648];'
         f'way["highway"]["highway"!~"^({ROAD_EXCLUDE})$"]({bb})->.w;'
         f'.w out body;node(w);out skel;')
    body = overpass(q, name)
    doc = json.loads(body)  # валидация
    if "elements" not in doc:
        raise RuntimeError("no elements")
    save_part("roads", name, body)
    return name, True


def task_green_tile(t):
    name = f"g_{t[0]:.5f}_{t[1]:.5f}.json"
    if part_exists("green", name):
        return name, True
    bb = fmt_bbox(t)
    q = (f'[out:json][timeout:900][maxsize:2147483648];('
         f'way["leisure"~"^(park|garden|recreation_ground|playground|dog_park|nature_reserve)$"]({bb});'
         f'way["landuse"~"^(forest|meadow|grass|recreation_ground|village_green|cemetery|allotments|orchard)$"]({bb});'
         f'way["natural"~"^(wood|scrub|grassland|beach|sand|wetland|heath)$"]({bb});'
         f');out geom;')
    body = overpass(q, name)
    json.loads(body)
    save_part("green", name, body)
    return name, True


def task_water_tile(t):
    name = f"w_{t[0]:.5f}_{t[1]:.5f}.json"
    if part_exists("water", name):
        return name, True
    bb = fmt_bbox(t)
    q = (f'[out:json][timeout:900][maxsize:1073741824];('
         f'way["natural"="water"]({bb});'
         f'way["waterway"~"^(riverbank|river|stream|canal|ditch)$"]({bb});'
         f');out geom;')
    body = overpass(q, name)
    json.loads(body)
    save_part("water", name, body)
    return name, True


def task_landuse_tile(t):
    name = f"l_{t[0]:.5f}_{t[1]:.5f}.json"
    if part_exists("landuse", name):
        return name, True
    bb = fmt_bbox(t)
    q = (f'[out:json][timeout:900][maxsize:1073741824];('
         f'way["landuse"~"^(residential|commercial|industrial|retail|garages|railway|brownfield|construction|farmland|farmyard)$"]({bb});'
         f');out geom;')
    body = overpass(q, name)
    json.loads(body)
    save_part("landuse", name, body)
    return name, True


def task_bigrels(_):
    """Большие объекты, размеченные мультиполигонами (Кубань!), — relations."""
    name = "rels.json"
    if part_exists("rels", name):
        return name, True
    bb = fmt_bbox((BBOX_S, BBOX_W, BBOX_N, BBOX_E))
    q = (f'[out:json][timeout:900][maxsize:2147483648];('
         f'relation["natural"="water"]({bb});'
         f'relation["waterway"="riverbank"]({bb});'
         f'relation["landuse"~"^(forest|meadow|grass|residential|industrial|commercial)$"]({bb});'
         f'relation["leisure"~"^(park|garden|nature_reserve)$"]({bb});'
         f');out geom;')
    body = overpass(q, name)
    json.loads(body)
    save_part("rels", name, body)
    return name, True


def task_rails(_):
    name = "rails.json"
    if part_exists("rails", name):
        return name, True
    bb = fmt_bbox((BBOX_S, BBOX_W, BBOX_N, BBOX_E))
    q = (f'[out:json][timeout:900][maxsize:1073741824];'
         f'way["railway"~"^(rail|tram|narrow_gauge)$"]({bb});out geom;')
    body = overpass(q, name)
    json.loads(body)
    save_part("rails", name, body)
    return name, True


def task_poi(_):
    name = "poi.json"
    if part_exists("poi", name):
        return name, True
    bb = fmt_bbox((BBOX_S, BBOX_W, BBOX_N, BBOX_E))
    q = (f'[out:json][timeout:900][maxsize:1073741824];('
         f'nwr["name"]["railway"~"^(station|halt)$"]({bb});'
         f'nwr["name"]["amenity"~"^(theatre|hospital|university|college|courthouse|townhall|marketplace|stadium|arts_centre|cinema)$"]({bb});'
         f'nwr["name"]["shop"~"^(mall|department_store)$"]({bb});'
         f'nwr["name"]["tourism"~"^(museum|attraction|zoo|aquarium|gallery)$"]({bb});'
         f'nwr["name"]["leisure"~"^(stadium|sports_centre|ice_rink|water_park)$"]({bb});'
         f'nwr["name"]["historic"~"^(memorial|monument|fort|castle)$"]({bb});'
         f'nwr["name"]["aeroway"="aerodrome"]({bb});'
         f'nwr["name"]["office"="government"]({bb});'
         f');out center;')
    body = overpass(q, name)
    json.loads(body)
    save_part("poi", name, body)
    return name, True


def run_group(group, tasks, workers):
    tasks = list(tasks)
    log(f"=== {group}: {len(tasks)} items")
    done, fail = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, t): t for t, fn in tasks}
        for f in futs:
            try:
                f.result()
                done += 1
            except Exception as e:  # noqa
                fail += 1
                t = futs[f]
                log(f"  FAIL {group} {t}: {e}")
            if (done + fail) % 10 == 0:
                log(f"  [{group}] progress {done+fail}/{len(tasks)} (fail={fail})")
            if time.time() > DEADLINE:
                log(f"  [{group}] deadline reached, stopping group")
                break
    log(f"=== {group} done={done} fail={fail}")
    return fail


def finalize():
    """Мёрж частей в компактные merged-файлы + manifest."""
    mg = os.path.join(OUT_DIR, "merged")
    os.makedirs(mg, exist_ok=True)
    man = {}

    # buildings: строки id|S,W,N,E|levels|type — дедуп по id
    seen = set()
    out_path = os.path.join(mg, "buildings.jsonl")
    with open(out_path, "w", encoding="utf-8") as out:
        pd = os.path.join(OUT_DIR, "parts", "buildings")
        if os.path.isdir(pd):
            for fn in sorted(os.listdir(pd)):
                with open(os.path.join(pd, fn), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        i = line.split("|", 1)[0]
                        if i in seen:
                            continue
                        seen.add(i)
                        out.write(line + "\n")
    man["buildings"] = len(seen)

    # остальные группы — просто конкатенация JSON-элементов с дедупом по (type,id)
    def merge_json(group, outname):
        seen = set()
        count = 0
        with open(os.path.join(mg, outname), "w", encoding="utf-8") as out:
            pd = os.path.join(OUT_DIR, "parts", group)
            if not os.path.isdir(pd):
                man[group] = 0
                return
            for fn in sorted(os.listdir(pd)):
                p = os.path.join(pd, fn)
                if os.path.getsize(p) == 0:
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        doc = json.load(f)
                except Exception as e:  # noqa
                    log(f"  [merge {group}] bad json {fn}: {e}")
                    continue
                for el in doc.get("elements", []):
                    k = (el.get("type"), el.get("id"))
                    if k in seen:
                        continue
                    seen.add(k)
                    out.write(json.dumps(el, ensure_ascii=False,
                                         separators=(",", ":")) + "\n")
                    count += 1
        man[group] = count

    merge_json("roads", "roads.jsonl")
    merge_json("green", "green.jsonl")
    merge_json("water", "water.jsonl")
    merge_json("landuse", "landuse.jsonl")
    merge_json("rels", "rels.jsonl")
    merge_json("rails", "rails.jsonl")
    merge_json("poi", "poi.jsonl")

    man["bbox"] = [BBOX_S, BBOX_W, BBOX_N, BBOX_E]
    man["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(mg, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    log("manifest:", json.dumps(man))


def main():
    os.makedirs(os.path.join(OUT_DIR, "parts"), exist_ok=True)
    t0 = time.time()
    log("TheK OSM fetch — Krasnodar, bbox", (BBOX_S, BBOX_W, BBOX_N, BBOX_E))

    fails = 0
    # сначала самое важное
    fails += run_group("buildings", [(t, task_building_tile) for t in grid(0.08)], 4)
    fails += run_group("roads", [(t, task_road_tile) for t in grid(0.045)], 4)
    fails += run_group("water", [(t, task_water_tile) for t in grid(0.135)], 4)
    fails += run_group("rels", [(None, task_bigrels)], 1)
    fails += run_group("green", [(t, task_green_tile) for t in grid(0.09)], 4)
    fails += run_group("landuse", [(t, task_landuse_tile) for t in grid(0.18)], 4)
    fails += run_group("rails", [(None, task_rails)], 1)
    fails += run_group("poi", [(None, task_poi)], 1)

    finalize()
    dt = time.time() - t0
    log(f"DONE in {dt/60:.1f} min, group failures: {fails}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
