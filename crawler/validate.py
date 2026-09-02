# -*- coding: utf-8 -*-
"""발행 전 관문. 형식은 멀쩡한데 값이 망가진 데이터를 앱까지 흘려보내지 않는다."""
import glob
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(BASE), "data")
MIN_FILES = 200          # 269개 중 일부는 거래가 없을 수 있다


def fail(msg):
    print(f"[검증 실패] {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    total = 0
    for folder in sorted(os.listdir(DATA)):
        d = os.path.join(DATA, folder)
        if not os.path.isdir(d):
            continue
        files = glob.glob(os.path.join(d, "*.json"))
        if len(files) < MIN_FILES:
            fail(f"{folder}: 파일 {len(files)}개 — 최소 {MIN_FILES}개 기대")
        rows = zero = neg = 0
        for f in files:
            j = json.load(open(f, encoding="utf-8"))
            if not j.get("updatedAt"):
                fail(f"{f}: updatedAt 없음")
            for r in j.get("rows", []):
                rows += 1
                if r["med"] == 0:
                    zero += 1
                if r["med"] < 0 or r["lo"] > r["hi"]:
                    neg += 1
        if rows == 0:
            fail(f"{folder}: 집계 0행 — 파싱이 깨졌을 때 나오는 전형적 증상")
        if zero:
            fail(f"{folder}: 금액 0인 행 {zero}개")
        if neg:
            fail(f"{folder}: 음수·역전 행 {neg}개")
        print(f"  - {folder}: 파일 {len(files)}개 / {rows:,}행 OK")
        total += rows
    print(f"검증 통과 (총 {total:,}행)")


if __name__ == "__main__":
    main()
