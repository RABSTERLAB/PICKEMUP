import sys, json, time
import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / "analysis-core"), str(ROOT / "packages" / "connectors")]
from pickemup_connectors.synthetic import SyntheticCampaignConnector
from pickemup_analysis.coordination import detect_coordination
from pickemup_analysis.automation import score_account
from pickemup_analysis.propagation import group_narrative
from pickemup_analysis.graph import account_graph
from collections import defaultdict

t0=time.time()
b = SyntheticCampaignConnector().run({})
gt = b.provenance["account_ground_truth"]
print(f"corpus: {len(b.accounts)} accounts, {len(b.posts)} posts  [{time.time()-t0:.1f}s]")

t0=time.time()
reports, edges, acts = detect_coordination(b.accounts, b.posts)
print(f"coordination: {len(edges)} edges, {len(reports)} clusters  [{time.time()-t0:.1f}s]")
for r in reports:
    gts = defaultdict(int)
    for m in r.members: gts[gt.get(m,"?")]+=1
    print(f"  {r.cluster_id}: {r.accounts} accts, signals={r.signal_mix}, conf={r.confidence}, GT={dict(gts)}")

# automation per account vs ground truth
from pickemup_analysis.evaluate import binary_eval
by_acct = defaultdict(list)
for p in b.posts: by_acct[p.author_id].append(p)
amap = {a.account_id:a for a in b.accounts}
pred_auto=set(); res={}
t0=time.time()
for a in b.accounts:
    r = score_account(a.account_id, by_acct.get(a.account_id,[]), {"default_profile": a.default_profile})
    res[a.account_id]=r
    if r.verdict=="likely_automated": pred_auto.add(a.account_id)
truth_auto = {aid for aid,l in gt.items() if l=="automated_coordinated"}
ev = binary_eval("automation", truth_auto, pred_auto, set(gt))
print(f"automation scoring [{time.time()-t0:.1f}s]", ev.as_dict())
# benign bots SHOULD score automated (they are) - check they exist & score>55
bb=[a.account_id for a in b.accounts if a.ground_truth_label=="benign_automated"]
print("benign bots verdicts:", {res[x].verdict for x in bb if x in res})
# power users must NOT be likely_automated
pu={a.account_id for a in b.accounts if a.ground_truth_label=="benign_human_active"}
print("power-user false automations:", len(pred_auto & pu))

t0=time.time()
qposts=[p for p in b.posts if p.text and "movement everyone" in p.text]
ng = group_narrative(qposts, corpus=b.posts)
print(f"propagation: {len(ng)} narrative groups  [{time.time()-t0:.1f}s]")
if ng:
    g=ng[0]; print("  first observed:", g.first_observed.post_id, g.first_observed.author_id, "posts:", len(g.posts), "amplifiers:", g.amplifiers[:3])

snap = account_graph(b.accounts, b.posts, edges, reports)
print("graph snapshot:", snap.stats)
