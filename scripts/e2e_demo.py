"""E2E acceptance: demo ingest -> analysis -> network -> narratives -> case -> report."""
import sys, time, json
import httpx

BASE = "http://127.0.0.1:8090"
c = httpx.Client(timeout=30)

def main():
    h = c.get(f"{BASE}/health").json()
    print("health:", h["status"], "| db:", h["db"]["backend"], "| worker alive:", h["worker"]["alive"])
    print("counts:", h["counts"])

    r = c.post(f"{BASE}/api/v1/demo/load", json={"seed": 1337}).json()
    ids = [j["id"] for j in r["enqueued"]]
    print("demo jobs:", ids)
    for _ in range(60):
        jobs = {j["id"]: j for j in c.get(f"{BASE}/api/v1/jobs").json()}
        st = {i: jobs[i]["status"] for i in ids if i in jobs}
        if st and all(v in ("done", "dead") for v in st.values()) and len(st) == len(ids):
            break
        time.sleep(2)
    for i in ids:
        j = jobs[i]
        print(f"  job {j['id']} {j['kind']}: {j['status']} {str(j['result'])[:120]} {('ERR:' + str(j['error']))[:150] if j['error'] else ''}")
    if any(jobs[i]["status"] == "dead" for i in ids):
        sys.exit("jobs failed")

    ov = c.get(f"{BASE}/api/v1/overview").json()
    print("\noverview accounts_by_origin:", ov["accounts_by_origin"], "spend:", ov["api_spend_usd_by_day"])
    co = c.get(f"{BASE}/api/v1/analysis/coordination").json()
    print(f"clusters ({co['algo_version']}):")
    for cl in co["clusters"]:
        print(f"  {cl['cluster_id']}: {cl['accounts']} accts verdict={cl['verdict']} conf={cl['confidence']} organic_notes={len(cl['organic_explanations'])}")

    g = c.get(f"{BASE}/api/v1/analysis/graph").json()
    print("graph:", g["stats"], "legend observed/inferred:", g["legend"])

    nar = c.get(f"{BASE}/api/v1/analysis/narratives").json()
    for n in nar[:2]:
        fo = n["first_observed"]
        print(f"narrative {n['narrative_id']} q='{n['query']}': {n['post_count']} posts, first={fo['post_id']} by {fo['author_id']} | gaps={len(n['coverage_gaps'])}")

    # account detail with explainable score
    accs = c.get(f"{BASE}/api/v1/accounts", params={"verdict": "likely_automated", "limit": 3}).json()
    print("\nlikely_automated sample:", [a["username"] for a in accs])
    if accs:
        d = c.get(f"{BASE}/api/v1/accounts/{accs[0]['account_id']}").json()
        print("  signals:", [s["name"] for s in d["automation"].get("signals", [])][:6],
              "| clusters:", d["cluster_memberships"])

    # case flow
    cid = c.post(f"{BASE}/api/v1/cases", json={"title": "Synthetic rally amplification demo",
                  "scope": "E2E acceptance on synthetic fixtures", "analyst": "e2e"}).json()["case_id"]
    cluster_id = co["clusters"][0]["cluster_id"] if co["clusters"] else None
    top_post = None
    ps = c.get(f"{BASE}/api/v1/posts", params={"q": "movement everyone", "limit": 1}).json()
    if ps: top_post = ps[0]["post_id"]
    c.post(f"{BASE}/api/v1/cases/{cid}/members", json={"kind": "cluster", "ref_id": cluster_id})
    if top_post:
        ev = c.get(f"{BASE}/api/v1/posts/{top_post}/evidence").json()
        print("evidence record:", {k: str(ev[k])[:40] for k in ('content_sha256','origin_mode','source_url')})
        c.post(f"{BASE}/api/v1/cases/{cid}/members", json={"kind": "post", "ref_id": top_post})
    c.post(f"{BASE}/api/v1/cases/{cid}/notes", json={"text": "Cluster C1 clockwork cadence + synced reposts; synthetic demo."})
    c.put(f"{BASE}/api/v1/cases/{cid}", json={"findings": "Demonstrates full pipeline on labeled synthetic data.", "status": "review"})
    rep = c.get(f"{BASE}/api/v1/cases/{cid}/report").json()
    print(f"\ncase {cid}: {len(rep['evidence'])} evidence items, manifest sha256={rep['evidence_manifest_sha256'][:24]}...")
    h = c.get(f"{BASE}/api/v1/cases/{cid}/report.html")
    assert "PICKEMUP report" in h.text and "Evidence" in h.text
    print("html report:", len(h.text), "bytes OK")

    al = c.get(f"{BASE}/api/v1/alerts").json()
    print("alerts:", [(a["kind"], a["subject_id"], a["severity"]) for a in al][:5])
    src = c.get(f"{BASE}/api/v1/sources").json()
    print("sources:", {k: v["available"] for k, v in src.items()})
    print("\nE2E: PASS")

if __name__ == "__main__":
    main()
