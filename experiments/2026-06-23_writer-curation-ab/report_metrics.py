"""보고서 셀 비교용 정량 지표 추출기 — 모든 A/B 셀에 동일 적용."""
import json, sys
from collections import Counter

def summarize(path, label=""):
    r = json.load(open(path))
    val = r.get("valuation", {})
    claims = r.get("claims", {})
    kinds = Counter(c.get("kind") for c in claims.values())
    aud = r.get("audit", {})
    mech = aud.get("mechanical", {})
    llm = aud.get("llm", {})
    issues = llm.get("issues", [])
    sev = Counter(i.get("severity") for i in issues)
    print(f"\n{'='*60}\n[{label}]  {path.split('/')[-2]}")
    print(f"  종합등급: {val.get('overall_grade')}  | 축: " +
          " ".join(f"{a['axis']}={a['grade']}" for a in val.get("axis_scores", [])))
    print(f"  주장: {len(claims)}  (factual={kinds.get('factual',0)} opinion={kinds.get('opinion',0)} gap={kinds.get('gap',0)})")
    print(f"  인용근거: {len(r.get('evidence',{}))}")
    print(f"  기계감사 위반: { {k:len(v) for k,v in mech.items() if v} or '없음'}")
    print(f"  LLM감사: pass={llm.get('overall_pass')}  이슈={len(issues)} (high={sev.get('high',0)} med={sev.get('medium',0)} low={sev.get('low',0)})")
    for i in issues:
        print(f"     - [{i.get('severity')}/{i.get('kind')}] {i.get('location','')[:40]}")
    # 섹션 깊이
    for sec in r.get("sections", []):
        nb = len(sec.get("blocks", []))
        nc = sum(len(b.get("markdown","")) for b in sec.get("blocks", []))
        nclm = sum(len(b.get("claim_ids",[])) for b in sec.get("blocks", []))
        print(f"  · {sec.get('axis'):10} {nb}블록 {nclm}주장 {nc}자")
    return r

if __name__ == "__main__":
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    summarize(sys.argv[1], label)
