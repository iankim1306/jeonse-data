# -*- coding: utf-8 -*-
"""
국토교통부 실거래가 → 전세보증금 앱용 집계 데이터.

  소스   공공데이터포털 국토교통부 실거래가 6종 (이용허락범위 제한 없음)
  주기   하루 1회. 최근 3개월은 신고 기한(계약일+30일) 때문에 계속 차오르므로 다시 받는다.
  출력   data/{종류}/{시군구코드}.json  — 앱은 자기 시군구 파일 하나(약 30KB)만 받는다

🔴 원본을 그대로 배포하지 않는다.
   12개월 전량이면 280만건·242MB 라 매일 커밋하면 git 이력이 감당이 안 된다.
   앱이 필요한 건 전 거래 내역이 아니라 "내 보증금과 비교할 시세"뿐이라
   법정동 × 면적대 × 월 단위 중앙값으로 접는다(1/50 로 줄어든다).
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(BASE), "data")

# 신고 기한이 계약일+30일이라 최근 달은 미완성이다. 3개월을 겹쳐 다시 받는다.
REFRESH_MONTHS = 3
KEEP_MONTHS = 12

# 동시 호출. API 허용은 30 TPS 인데 절반만 쓴다(장애 시 상대 서버 부담을 줄인다).
CONC = 8

SOURCES = [
    # (출력 폴더, 서비스, 오퍼레이션, 금액 필드)
    ("rent_apt",  "RTMSDataSvcAptRent",   "getRTMSDataSvcAptRent",   "deposit"),
    ("rent_rh",   "RTMSDataSvcRHRent",    "getRTMSDataSvcRHRent",    "deposit"),
    ("rent_offi", "RTMSDataSvcOffiRent",  "getRTMSDataSvcOffiRent",  "deposit"),
    ("rent_sh",   "RTMSDataSvcSHRent",    "getRTMSDataSvcSHRent",    "deposit"),
    ("trade_apt", "RTMSDataSvcAptTrade",  "getRTMSDataSvcAptTrade",  "dealAmount"),
    ("trade_rh",  "RTMSDataSvcRHTrade",   "getRTMSDataSvcRHTrade",   "dealAmount"),
]


def kst_now():
    return datetime.now(timezone(timedelta(hours=9)))


def env_key():
    k = os.environ.get("DATA_GO_KR_KEY", "")
    if k:
        return k
    p = os.path.join(BASE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if line.startswith("DATA_GO_KR_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DATA_GO_KR_KEY 없음 (.env 또는 환경변수)")


KEY = env_key()


def months(n, end=None):
    """최근 n개월 (YYYYMM), 오래된 것부터"""
    d = end or kst_now()
    out = []
    for _ in range(n):
        out.append(f"{d.year}{d.month:02d}")
        d = (d.replace(day=1) - timedelta(days=1))
    return list(reversed(out))


def http(url, retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.6 * (i + 1))
    return ""


def fetch(svc, op, lawd, ymd):
    """한 시군구·한 달 전량. 1000건씩 페이지를 넘긴다."""
    out = []
    for pg in range(1, 30):
        q = urllib.parse.urlencode({
            "serviceKey": KEY, "LAWD_CD": lawd, "DEAL_YMD": ymd,
            "numOfRows": 1000, "pageNo": pg,
        }, safe="%")
        t = http(f"https://apis.data.go.kr/1613000/{svc}/{op}?{q}")
        auth = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", t)
        if auth:
            raise RuntimeError(f"{svc} {lawd}/{ymd}: {auth.group(1)}")
        for it in re.findall(r"<item>(.*?)</item>", t, re.S):
            out.append(dict(re.findall(r"<(\w+)>\s*([^<]*)</\1>", it)))
        tot = re.search(r"<totalCount>(\d+)</totalCount>", t)
        if not tot or pg * 1000 >= int(tot.group(1)):
            break
    return out


def area_band(a):
    """면적대 — 같은 동네라도 평수가 다르면 시세가 다르다"""
    try:
        a = float(a)
    except (TypeError, ValueError):
        return "?"
    if a < 40:
        return "~40"
    if a < 60:
        return "40~60"
    if a < 85:
        return "60~85"
    return "85~"


def aggregate(rows, amount_field, rent_only_jeonse):
    """법정동 × 면적대 × 월 → 중앙값·건수. 원본 거래는 버린다.

    🔴 전월세 자료에는 전세와 월세가 섞여 있다. 월세 보증금까지 같이 넣으면
       중앙값이 아래로 끌려 내려가 '내 보증금이 시세보다 훨씬 높다'는 잘못된
       경고가 나온다. 그래서 순수 전세(월세 0)만 집계한다.
    """
    bucket = {}
    for d in rows:
        if rent_only_jeonse:
            mr = (d.get("monthlyRent") or "").replace(",", "").strip()
            if not mr.isdigit() or int(mr) != 0:
                continue
        v = (d.get(amount_field) or "").replace(",", "").strip()
        if not v.isdigit() or int(v) <= 0:      # 보증금 0 은 비교 대상이 아니다
            continue
        y, m = d.get("dealYear"), d.get("dealMonth")
        if not y or not m:
            continue
        k = (d.get("umdNm", "").strip(), area_band(d.get("excluUseAr")),
             f"{y}{int(m):02d}")
        bucket.setdefault(k, []).append(int(v))
    out = []
    for (dong, band, ym), vals in bucket.items():
        vals.sort()
        out.append({
            "d": dong, "a": band, "m": ym, "n": len(vals),
            "med": int(statistics.median(vals)),
            "lo": vals[0], "hi": vals[-1],
        })
    out.sort(key=lambda r: (r["d"], r["a"], r["m"]))
    return out


def load_prev(path):
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, encoding="utf-8")).get("rows", [])
    except Exception:
        return []


def merge(prev, fresh, keep):
    """최근 3개월은 새 값으로 덮고, 그 이전은 기존 값을 유지한다."""
    fresh_months = {r["m"] for r in fresh}
    kept = [r for r in prev if r["m"] not in fresh_months and r["m"] in keep]
    return sorted(kept + [r for r in fresh if r["m"] in keep],
                  key=lambda r: (r["d"], r["a"], r["m"]))


def main():
    sgg = json.load(open(os.path.join(BASE, "sgg.json"), encoding="utf-8"))
    bootstrap = "--bootstrap" in sys.argv
    refresh = months(KEEP_MONTHS if bootstrap else REFRESH_MONTHS)
    keep = set(months(KEEP_MONTHS))
    now = kst_now().strftime("%Y-%m-%d %H:%M")
    print(f"시군구 {len(sgg)}개 · 갱신 {len(refresh)}개월"
          f"{' (최초 백필)' if bootstrap else ''} · 보관 {len(keep)}개월")

    failures = []
    for folder, svc, op, amount in SOURCES:
        d = os.path.join(OUT, folder)
        os.makedirs(d, exist_ok=True)
        t0 = time.time()

        def work(code):
            try:
                raw = []
                for ym in refresh:
                    raw += fetch(svc, op, code, ym)
                return code, aggregate(raw, amount, folder.startswith("rent_")), None
            except Exception as e:                       # noqa: BLE001
                return code, None, f"{type(e).__name__}: {e}"

        done = errs = 0
        with ThreadPoolExecutor(max_workers=CONC) as ex:
            for code, fresh, err in ex.map(work, sgg):
                if err:
                    errs += 1
                    failures.append(f"{folder}/{code} {err}")
                    continue
                path = os.path.join(d, f"{code}.json")
                rows = merge(load_prev(path), fresh, keep)
                json.dump({"updatedAt": now, "sgg": code, "name": sgg[code],
                           "source": "국토교통부 실거래가 (공공데이터포털)",
                           "count": len(rows), "rows": rows},
                          open(path, "w", encoding="utf-8"),
                          ensure_ascii=False, separators=(",", ":"))
                done += 1
        print(f"[{'OK ' if not errs else 'WARN'}] {folder:10} {done}/{len(sgg)}개 "
              f"({time.time() - t0:.0f}초)" + (f" 실패 {errs}" if errs else ""))

    json.dump({"updatedAt": now, "months": sorted(keep),
               "sources": [s[0] for s in SOURCES], "sgg": sgg},
              open(os.path.join(OUT, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    if failures:
        print(f"\n실패 {len(failures)}건", file=sys.stderr)
        for f in failures[:20]:
            print("  " + f, file=sys.stderr)
        # 일부 시군구 실패는 흔하다(거래 0건 지역 등). 절반 넘게 깨지면 실패로 본다.
        if len(failures) > len(sgg) * len(SOURCES) * 0.5:
            sys.exit(1)
    print("\n완료")


if __name__ == "__main__":
    main()
