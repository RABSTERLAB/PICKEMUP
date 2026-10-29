"""Synthetic coordinated-campaign generator (demo dataset, directive §23).

Produces a fully labeled corpus exercising every engine:
  - botswarm      : automated coordinated amplifiers (clockwork + near-dup + synced reposts)
  - astroturf     : human-operated coordinated group (varied text, timed reposts, sleep windows)
  - benign_power  : very active HUMANS (must NOT be flagged as automation by volume)
  - benign_bots   : legitimate automated feeds (automated but NOT coordinated)
  - organic_news  : independent accounts covering one breaking event (must NOT all cluster)
  - independents  : background noise
All names invented, content safe, domains RFC2606 (example.*). Arabic + English + Arabizi.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from pickemup_analysis.domain import AccountObs, PostObs

from . import Connector, ConnectorManifest, IngestBatch

GROUPS = {
    "botswarm": dict(prefix="synthbot", n=25, gt="automated_coordinated"),
    "astroturf": dict(prefix="synthturf", n=12, gt="human_coordinated"),
    "benign_power": dict(prefix="synthnewsjunkie", n=6, gt="benign_human_active"),
    "benign_bots": dict(prefix="synthfeed", n=3, gt="benign_automated"),
    "organic_news": dict(prefix="synthcitizen", n=40, gt="organic"),
    "independents": dict(prefix="synthus", n=15, gt="benign_human"),
}

BOT_TEXTS = [
    "This is the movement everyone is talking about! Join NOW #{tag} ➡ {url} #{tag2}",
    "Do not miss it — {url} #{tag} #{tag2} the truth spreads fast",
    "We are thousands strong. #{tag} #{tag2} {url}",
]
TURF_TEMPLATES = [
    "My neighbours on my street also put up the {tag2} flag today. Finally someone listens.",
    " attended a {tag2} gathering near the municipal square. Organised, peaceful, many families.",
    "3ayni hada el-wa9t yiddu 7o 7awel {tag2} (# {tag}).",  # arabizi
    "الناس في حيّنا كلها تدعم #{tag} — صوتنا وصل أخيراً",       # MSA/Levantine
    "Why does nobody report on the {tag2} town-hall meetings? I went to two.",
    "شفت بعيني تنظيم رائع في وقفة #{tag} أمس، ناس محترمة وعائلات",
]
NEWS_TEXTS = [
    "Breaking: Minister of Health inaugurates the new children's hospital in {city} today.",
    "Long queue outside the new hospital; my cousin had her appointment this morning. #{tag}",
    "الوزير افتتح مستشفى الأطفال الجديد اليوم في {city} #{tag}",
    "Council confirms the hospital opens to the public next week. {url}",
    "Was at the opening — band, speeches, also actual doctors on staff. #{tag}",
]
JUNKIE = [
    "Morning roundup: 9 stories you missed — {url}",
    "Live thread on the transit strike, updating every 20 min {url}",
    "Weather alert, markets close, sports scores: today's digest {url}",
]
# Fictional-target, non-graphic risk exemplars so the bilingual classifier has
# something real to catch in demo data (ground-truth flagged via gt='risk-*').
RISKY_TEXTS = [
    "those subhuman newcomers are vermin crawling through our clean districts every one of them",
    "دم الوافدين الجدد رخيص في حيّنا اقتلوهم إن رفضوا المغادرة الليلة",
    "why do we still tolerate that filthy breed near our schools? drive them out, all of them",
    "هؤلاء القذرون يحتلون شوارعنا — طردوهم جميعا ولا بقاء لهم بيننا الليلة",
]
FEED = ["Hourly feed update {n}: air quality index for district {d} — details {url}"]

# Macro-region centers for synthetic account attribution (clearly-labeled ESTIMATED
# in demo mode; live connectors populate only from public profile location strings).
REGIONS = {
    "NA_EAST": (38.9, -77.0), "LATAM": (-15.8, -47.9), "NW_EU": (51.5, -0.1),
    "E_EU": (50.4, 30.5), "LEVANT": (33.9, 35.5), "GULF": (25.3, 51.2),
    "MAGHREB": (31.8, -3.7), "SAHEL": (12.0, 8.5), "WAFRICA": (6.5, 3.4),
    "SASIA": (28.6, 77.2), "SEA": (1.35, 103.8), "EASIA": (35.7, 139.7),
    "OCEANIA": (-33.9, 151.2),
}
GROUP_REGIONS = {
    "botswarm": ["NA_EAST", "SEA", "EASIA", "SASIA"],
    "astroturf": ["LEVANT", "GULF", "MAGHREB"],
    "benign_power": ["NW_EU", "NA_EAST", "OCEANIA", "E_EU"],
    "benign_bots": ["NA_EAST", "EASIA"],
    "organic_news": ["LEVANT", "NW_EU", "GULF"],
    "independents": ["NA_EAST", "LATAM", "SAHEL", "SEA", "NW_EU", "MAGHREB"],
}


class SyntheticCampaignConnector(Connector):
    dev_only = True
    manifest = ConnectorManifest(
        name="synthetic_campaign", platform="synthetic",
        capabilities=["generate"], auth_required=False,
        rate_limit_desc="n/a (local generation)", cost_model="free",
        coverage_gaps=["not real-world data; behavior patterns are textbook examples"],
        retention_rule="regenerable at any time; safe to delete",
        data_freshness="generated-at-run-time",
    )

    def available(self):
        return True, "local generator; always available (clearly labeled synthetic)"

    def run(self, params: dict, budget=None) -> IngestBatch:
        seed = int(params.get("seed", 1337))
        rnd = random.Random(seed)
        t0 = datetime.now(timezone.utc) - timedelta(hours=72)
        accounts: list[AccountObs] = []
        posts: list[PostObs] = []
        pid = 0

        def mk_acct(group: str, i: int) -> AccountObs:
            cfg = GROUPS[group]
            uid = f"{cfg['prefix']}{i:03d}"
            return AccountObs(account_id=f"acct-{uid}", username=uid,
                              display_name=uid.replace("synth", "Synth ").title(),
                              created_at=t0 - timedelta(days=rnd.randint(5, 900 if group != "botswarm" else 40)),
                              followers=rnd.randint(10, 800) if group == "botswarm" else rnd.randint(50, 40000),
                              default_profile=group == "botswarm",
                              region=rnd.choice(GROUP_REGIONS.get(group, ["NA_EAST"])),
                              origin_mode="synthetic", ground_truth_label=cfg["gt"])

        def mk_post(a: AccountObs, at: datetime, text, tags=(), urls=(), mentions=(),
                    repost_of=None, retweets=None):
            nonlocal pid
            pid += 1
            return PostObs(post_id=f"post-{pid:05d}", author_id=a.account_id, text=text,
                           created_at=at, collected_at=self.now(), lang=rnd.choice(["en", "ar"]),
                           hashtags=list(tags), urls=list(urls), mentions=list(mentions),
                           repost_of=repost_of, retweet_count=retweets, origin_mode="synthetic",
                           source_url=f"https://x.com/{a.username}/status/{pid}")

        tag, tag2 = "SyntheticRally", "TogetherWeRise"
        rally_url = "https://example.org/synthetic-rally"
        news_tag = "NewChildrensHospital"
        cities = ["Northfield", "Riverton", "Harbourview"]
        # deterministic inter-group ties (co-targeted campaign: swarm amplifies turf
        # posts, turf members reply to swarm) — creates cross-region interactions
        CROSS = [(a, b) for a, b in zip(range(0, 20, 4), range(0, 20, 5))]

        # --- 0. astroturf seed created first: the bot cell amplifies it cross-region ---
        turf = [mk_acct("astroturf", i) for i in range(GROUPS["astroturf"]["n"])]
        turf_seed = mk_post(turf[0], t0 + timedelta(hours=30),
                            TURF_TEMPLATES[3].format(tag=tag), tags=(tag,))

        # --- 1. botswarm: clockwork near-duplicate amplification ---
        swarm = [mk_acct("botswarm", i) for i in range(GROUPS["botswarm"]["n"])]
        seeds = [mk_post(swarm[0], t0, BOT_TEXTS[0].format(tag=tag, tag2=tag2, url=rally_url),
                         tags=(tag, tag2), urls=[rally_url], retweets=400)]
        for k, a in enumerate(swarm):
            base = t0 + timedelta(minutes=5 * k)
            for j in range(12):
                at = base + timedelta(minutes=12 * j, seconds=rnd.uniform(-25, 25))  # clockwork ±25s
                if j % 3 == 2 and rnd.random() < 0.9:
                    posts.append(mk_post(a, at, "", repost_of=seeds[0].post_id, retweets=0))
                else:
                    # ~7% of swarm text posts carry risk-laden frames (fictional target);
                    # index by account so AR and EN variants both plant
                    if (k + j) % 14 == 0:
                        posts.append(mk_post(a, at, RISKY_TEXTS[k % len(RISKY_TEXTS)],
                                             tags=(tag,), urls=[]))
                    elif (k, j) in [(i, 4) for i, _ in CROSS]:
                        # bot cell amplifies the human-organized cell's seed (cross-region)
                        posts.append(mk_post(a, at + timedelta(minutes=37), "",
                                             repost_of=turf_seed.post_id, retweets=0))
                    else:
                        posts.append(mk_post(a, at,
                            BOT_TEXTS[j % 3].format(tag=tag, tag2=tag2, url=rally_url),
                            tags=(tag, tag2), urls=[rally_url]))
        posts.extend(seeds)

        # --- 2. astroturf: coordinated humans (varied text, timed reposts, sleep) ---
        posts.append(turf_seed)
        for k, a in enumerate(turf[1:], 1):
            for j in range(5):
                # same daily event window; pairs post near each other (14:00-18:00)
                at = t0 + timedelta(days=j, hours=14 + (k % 4), minutes=rnd.randint(0, 45))
                if j == 0 and rnd.random() < 0.85:
                    posts.append(mk_post(a, t0 + timedelta(hours=30, minutes=rnd.randint(3, 25)),
                                         "", repost_of=turf_seed.post_id))
                else:
                    tmpl = TURF_TEMPLATES[j % len(TURF_TEMPLATES)]
                    if (k, j) in [(b, 2) for _, b in CROSS if b < len(turf) and k < len(swarm)]:
                        # turf member replies into the bot cell (cross-region mention)
                        posts.append(mk_post(a, at, f"agree @{swarm[k].username} about {tag}",
                                             tags=(tag,), mentions=[swarm[k].username]))
                    else:
                        posts.append(mk_post(a, at, tmpl.format(tag=tag, tag2=tag2),
                                             tags=(tag,), urls=[rally_url] if rnd.random() < 0.3 else []))

        # --- 3. benign_power: very active humans, unique content, random bursts ---
        for i in range(GROUPS["benign_power"]["n"]):
            a = mk_acct("benign_power", i)
            accounts.append(a)
            n = rnd.randint(35, 55)
            for j in range(n):
                at = t0 + timedelta(minutes=rnd.uniform(0, 72 * 60))
                posts.append(mk_post(a, at, JUNKIE[j % 3].format(url=f"https://example.net/digest{i}{j}"),
                                     urls=[f"https://example.net/digest{i}{j}"]))

        # --- 4. benign_bots: legitimate automated feeds (not coordinated) ---
        for i in range(GROUPS["benign_bots"]["n"]):
            a = mk_acct("benign_bots", i)
            accounts.append(a)
            for j in range(30):
                at = t0 + timedelta(hours=2 * j, minutes=rnd.uniform(-5, 5))   # periodic, DIFFERENT cadence owners share nothing
                posts.append(mk_post(a, at, FEED[0].format(n=j, d=i + 1, url=f"https://example.com/feed{i}"),
                                     urls=[f"https://example.com/feed{i}"]))

        # --- 5. organic_news: independent coverage of one event ---
        for i in range(GROUPS["organic_news"]["n"]):
            a = mk_acct("organic_news", i)
            accounts.append(a)
            for j in range(rnd.randint(1, 3)):
                at = t0 + timedelta(hours=20 + rnd.uniform(0, 40))
                tmpl = rnd.choice(NEWS_TEXTS)
                posts.append(mk_post(a, at,
                    tmpl.format(city=rnd.choice(cities), tag=news_tag,
                                url=f"https://example.org/news{i}"),
                    tags=(news_tag,), urls=[f"https://example.org/news{i}"] if rnd.random() < 0.5 else []))

        # --- 6. independents: background noise ---
        for i in range(GROUPS["independents"]["n"]):
            a = mk_acct("independents", i)
            accounts.append(a)
            for j in range(rnd.randint(1, 2)):
                posts.append(mk_post(a, t0 + timedelta(hours=rnd.uniform(0, 72)),
                                     rnd.choice(["quiet day at the bookshop",
                                                 "kafka on the shore again, chapter 7",
                                                 "مساء الخير، يوم هادئ", "test test"])))

        for grp, lst in (("botswarm", swarm), ("astroturf", turf)):
            accounts.extend(lst)
        gt = {a.account_id: a.ground_truth_label for a in accounts}
        # ground truth for every account incl. inline-created ones
        for a in accounts:
            gt[a.account_id] = a.ground_truth_label
        return IngestBatch(accounts=accounts, posts=posts, origin_mode="synthetic",
                           provenance={"source_kind": "synthetic_campaign_generator",
                                       "seed": seed, "generated_at": self.now().isoformat(),
                                       "group_ground_truth": GROUPS,
                                       "account_ground_truth": gt,
                                       "label": "SYNTHETIC DEMO DATA — not real-world observations"})
