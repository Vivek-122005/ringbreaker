"""Synthetic retail-bank dataset with planted financial-crime typologies.

Everything here is fake. Patterns follow publicly documented money-laundering
typologies (FATF / FinCEN advisories): authorised-push-payment scam cash-out
through money mules, round-tripping between shell companies, and synthetic
identity bust-out. Benign look-alikes (families sharing a home network,
payroll fan-out, landlords collecting rent) are planted on purpose so the
transaction-monitoring rules raise false positives, as they do in real banks.

Ground truth is written to a separate JSON file and never loaded into the
graph, so the investigator cannot peek at it.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone

DAY = 86_400
END = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
START = END - 90 * DAY

FIRST = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Riya", "Ananya", "Diya", "Meera", "Saanvi",
         "Arjun", "Rohan", "Neha", "Priya", "Karan", "Sneha", "Vikram", "Pooja", "Rahul", "Tanvi",
         "James", "Olivia", "Liam", "Emma", "Noah", "Ava", "Lucas", "Mia", "Ethan", "Zara",
         "Omar", "Leila", "Mateo", "Sofia", "Chen", "Mei", "Yusuf", "Amara", "Kofi", "Nia"]
LAST = ["Sharma", "Verma", "Gupta", "Iyer", "Nair", "Reddy", "Mehta", "Kapoor", "Singh", "Das",
        "Smith", "Jones", "Brown", "Taylor", "Wilson", "Khan", "Patel", "Garcia", "Silva", "Okafor",
        "Lee", "Wang", "Haddad", "Rossi", "Novak", "Kim", "Ali", "Mensah", "Costa", "Fischer"]
CITIES = ["Mumbai", "Pune", "Bengaluru", "Delhi", "Hyderabad", "Chennai", "Kolkata", "Jaipur"]
STREETS = ["MG Road", "Park Street", "Link Road", "Ring Road", "Station Road", "Lake View", "Hill Road"]
MERCHANTS = [
    ("grocery", 0.1, ["FreshMart", "DailyBasket", "GreenGrocer", "CityMart"]),
    ("utilities", 0.1, ["PowerGrid Energy", "AquaWater", "FiberNet"]),
    ("dining", 0.1, ["Spice Route", "Cafe Mocha", "Pizza Planet"]),
    ("ecommerce", 0.2, ["ShopKart", "MegaMall Online"]),
    ("travel", 0.2, ["SkyFly Air", "RailGo"]),
    ("gift_cards", 0.7, ["GiftHub Cards"]),
    ("crypto_exchange", 0.8, ["CoinVault Exchange", "BitBridge OTC"]),
    ("money_transfer", 0.6, ["QuickRemit"]),
]


@dataclass
class World:
    seed: int
    rng: random.Random
    persons: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    phones: dict = field(default_factory=dict)
    addresses: dict = field(default_factory=dict)
    devices: dict = field(default_factory=dict)
    ips: dict = field(default_factory=dict)
    merchants: list = field(default_factory=list)
    owns: list = field(default_factory=list)
    has_phone: list = field(default_factory=list)
    lives_at: list = field(default_factory=list)
    used_device: list = field(default_factory=list)
    accessed_from: list = field(default_factory=list)
    transfers: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    truth: list = field(default_factory=list)
    _n: dict = field(default_factory=dict)

    def nid(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}-{self._n[prefix]:05d}"

    # --- entity helpers -------------------------------------------------
    def phone(self) -> str:
        p = f"+91-9{self.rng.randint(100000000, 999999999)}"
        self.phones[p] = {"id": p}
        return p

    def address(self) -> str:
        a = self.nid("ADDR")
        self.addresses[a] = {"id": a, "line": f"{self.rng.randint(1, 400)} {self.rng.choice(STREETS)}",
                             "city": self.rng.choice(CITIES)}
        return a

    def device(self, os_name: str | None = None) -> str:
        d = self.nid("DEV")
        self.devices[d] = {"id": d, "os": os_name or self.rng.choice(["Android 15", "iOS 19", "Windows 11", "macOS 16"])}
        return d

    def ip(self, kind: str = "residential") -> str:
        r = self.rng
        addr = f"{r.randint(11, 223)}.{r.randint(0, 255)}.{r.randint(0, 255)}.{r.randint(1, 254)}"
        asn = {"residential": "AS9829 BSNL" if r.random() < .5 else "AS24560 Airtel",
               "mobile": "AS55836 Jio", "hosting": r.choice(["AS14061 DigitalOcean", "AS16276 OVH"]),
               "vpn": "AS9009 M247 (VPN)"}[kind]
        self.ips[addr] = {"id": addr, "asn": asn, "kind": kind}
        return addr

    def person(self, kyc_ts: int, name: str | None = None, phone: str | None = None,
               address: str | None = None, segment: str = "retail") -> str:
        pid = self.nid("P")
        r = self.rng
        self.persons.append({"id": pid, "name": name or f"{r.choice(FIRST)} {r.choice(LAST)}",
                             "kyc_date": kyc_ts, "segment": segment})
        self.has_phone.append((pid, phone or self.phone()))
        self.lives_at.append((pid, address or self.address()))
        return pid

    def account(self, owner: str, opened: int, kind: str = "savings", balance: float | None = None) -> str:
        aid = self.nid("ACC")
        self.accounts.append({"id": aid, "type": kind, "opened": opened, "status": "active",
                              "balance": round(balance if balance is not None else self.rng.uniform(2_000, 90_000), 2)})
        self.owns.append((owner, aid))
        return aid

    def transfer(self, src: str, dst: str, amount: float, ts: int, channel: str = "UPI"):
        self.transfers.append({"id": self.nid("TX"), "src": src, "dst": dst, "amount": round(amount, 2),
                               "ts": int(ts), "channel": channel})

    def pay(self, src: str, merchant: str, amount: float, ts: int):
        self.payments.append({"src": src, "dst": merchant, "amount": round(amount, 2), "ts": int(ts)})

    def merchant_of(self, category: str) -> str:
        return self.rng.choice([m["id"] for m in self.merchants if m["category"] == category])


def _normal_population(w: World, n_people: int):
    r = w.rng
    for cat, risk, names in MERCHANTS:
        for nm in names:
            w.merchants.append({"id": w.nid("M"), "name": nm, "category": cat, "risk": risk})
    everyday = [m["id"] for m in w.merchants if m["risk"] <= 0.2]

    customers = []
    for _ in range(n_people):
        kyc = START - r.randint(200, 3000) * DAY
        p = w.person(kyc)
        acc = w.account(p, kyc + r.randint(0, 30) * DAY)
        dev = w.device()
        home = w.ip("residential")
        mob = w.ip("mobile")
        w.used_device.append((acc, dev))
        w.accessed_from.append((acc, home))
        w.accessed_from.append((acc, mob))
        customers.append(acc)

    # everyday spending
    for acc in customers:
        for _ in range(r.randint(3, 9)):
            w.pay(acc, r.choice(everyday), r.uniform(150, 6_000), r.randint(START, END))
    # P2P between friends: small social circles
    for acc in customers:
        friends = r.sample(customers, 3)
        for f in friends:
            if f != acc and r.random() < 0.6:
                for _ in range(r.randint(1, 3)):
                    w.transfer(acc, f, r.uniform(100, 4_000), r.randint(START, END))
    # occasional legit crypto buyers so the merchant alone is not a verdict
    for acc in r.sample(customers, max(5, n_people // 60)):
        w.pay(acc, w.merchant_of("crypto_exchange"), r.uniform(2_000, 25_000), r.randint(START, END))
    return customers


def _benign_lookalikes(w: World, customers: list):
    r = w.rng
    # Families sharing one address, home router and a tablet.
    for _ in range(6):
        addr, home, tablet = w.address(), w.ip("residential"), w.device("Android 15 (tablet)")
        surname = r.choice(LAST)
        fam = []
        for _ in range(r.randint(3, 5)):
            p = w.person(START - r.randint(400, 3000) * DAY, name=f"{r.choice(FIRST)} {surname}", address=addr)
            a = w.account(p, START - r.randint(300, 2500) * DAY)
            w.used_device.append((a, tablet))
            w.accessed_from.append((a, home))
            fam.append(a)
        for a in fam:
            for b in fam:
                if a != b and r.random() < .5:
                    w.transfer(a, b, r.uniform(500, 15_000), r.randint(START, END))
        w.truth.append({"ring": f"FAMILY-{surname}", "typology": "benign_family", "accounts": fam})

    # Small businesses paying monthly salaries.
    for _ in range(4):
        owner = w.person(START - 2000 * DAY, segment="business")
        biz = w.account(owner, START - r.randint(900, 2500) * DAY, "business", 900_000)
        staff = r.sample(customers, r.randint(9, 16))
        for month in range(3):
            pay_day = START + (month * 30 + 28) * DAY + r.randint(0, 3600)
            for s in staff:
                w.transfer(biz, s, r.choice([18_000, 22_000, 26_000, 35_000]), pay_day, "NEFT")
        w.truth.append({"ring": f"PAYROLL-{biz}", "typology": "benign_payroll", "accounts": [biz]})

    # Landlords collecting rent from many tenants (classic fan-in false positive).
    for _ in range(3):
        ll = w.person(START - 2500 * DAY)
        lacc = w.account(ll, START - 2000 * DAY)
        tenants = r.sample(customers, r.randint(9, 12))
        for month in range(3):
            for t in tenants:
                w.transfer(t, lacc, r.choice([12_000, 15_000, 18_000]), START + (month * 30 + r.randint(1, 5)) * DAY)
        w.truth.append({"ring": f"LANDLORD-{lacc}", "typology": "benign_landlord", "accounts": [lacc]})


def _mule_network(w: World, customers: list, idx: int):
    """APP-scam proceeds: victims -> collector -> layering mules -> cash-out -> crypto."""
    r = w.rng
    t0 = END - r.randint(5, 40) * DAY
    op_devices = [w.device("Android 14 (emulator)") for _ in range(2)]
    op_ip = w.ip(r.choice(["vpn", "hosting"]))

    def mule(age_days: int) -> str:
        p = w.person(t0 - age_days * DAY - r.randint(0, 5) * DAY)
        a = w.account(p, t0 - age_days * DAY, balance=r.uniform(50, 900))
        w.used_device.append((a, r.choice(op_devices)))
        w.accessed_from.append((a, op_ip))
        w.accessed_from.append((a, w.ip("mobile")))
        return a

    collectors = [mule(r.randint(8, 25)) for _ in range(r.randint(1, 2))]
    layers = [mule(r.randint(10, 40)) for _ in range(r.randint(4, 7))]
    cashout = [mule(r.randint(20, 60)) for _ in range(r.randint(1, 2))]
    victims = r.sample(customers, r.randint(8, 14))

    ts = t0
    pot = {c: 0.0 for c in collectors}
    for v in victims:
        c = r.choice(collectors)
        amt = r.uniform(15_000, 120_000)
        ts += r.randint(600, 6 * 3600)
        w.transfer(v, c, amt, ts, "IMPS")
        pot[c] += amt
    for c, total in pot.items():
        t = ts + r.randint(600, 3 * 3600)
        remaining = total * 0.97
        while remaining > 1_000:
            amt = min(remaining, r.uniform(9_000, 9_950))  # structured under 10k reporting threshold
            t += r.randint(120, 1800)
            w.transfer(c, r.choice(layers), amt, t, "UPI")
            remaining -= amt
    lt = ts + 8 * 3600
    for l in layers:
        inbound = sum(x["amount"] for x in w.transfers if x["dst"] == l)
        lt += r.randint(300, 2400)
        w.transfer(l, r.choice(cashout), inbound * r.uniform(0.9, 0.96), lt, "IMPS")
    for co in cashout:
        inbound = sum(x["amount"] for x in w.transfers if x["dst"] == co)
        ct = lt + r.randint(1800, 7200)
        for _ in range(r.randint(2, 4)):
            w.pay(co, w.merchant_of(r.choice(["crypto_exchange", "crypto_exchange", "gift_cards"])),
                  inbound / 3 * r.uniform(.8, 1.0), ct)
            ct += r.randint(300, 3600)
    w.truth.append({"ring": f"MULE-{idx}", "typology": "app_scam_mule_network",
                    "accounts": collectors + layers + cashout, "victims": victims,
                    "roles": {"collector": collectors, "layering": layers, "cash_out": cashout}})


def _round_trip(w: World, idx: int):
    """Shell companies cycling the same funds to inflate turnover."""
    r = w.rng
    shared_addr = w.address()
    director_phone = w.phone()
    accs = []
    for k in range(r.randint(4, 6)):
        p = w.person(START - r.randint(100, 300) * DAY, name=f"{r.choice(['Apex', 'Nimbus', 'Vertex', 'Orion', 'Zenith', 'Helix', 'Quanta'])} "
                                                               f"{r.choice(['Traders', 'Exim', 'Ventures', 'Holdings', 'Logistics'])} Pvt Ltd",
                     phone=director_phone if k % 2 == 0 else None, address=shared_addr, segment="business")
        accs.append(w.account(p, START - r.randint(60, 200) * DAY, "business", r.uniform(5_000, 50_000)))
    for loop in range(r.randint(3, 5)):
        amt = r.uniform(400_000, 1_500_000)
        t = START + r.randint(5, 80) * DAY
        for i, a in enumerate(accs):
            b = accs[(i + 1) % len(accs)]
            t += r.randint(3600, 2 * DAY)
            w.transfer(a, b, amt, t, "RTGS")
            amt *= r.uniform(0.985, 0.998)
    w.truth.append({"ring": f"ROUNDTRIP-{idx}", "typology": "round_tripping", "accounts": accs})


def _synthetic_identity(w: World, idx: int):
    """Fabricated identities sharing phones/addresses, aged then busted out to one sink."""
    r = w.rng
    phones = [w.phone() for _ in range(2)]
    addrs = [w.address() for _ in range(2)]
    dev = w.device("Windows 11")
    opened = START + r.randint(0, 20) * DAY
    accs = []
    for _ in range(r.randint(5, 8)):
        p = w.person(opened - r.randint(0, 6) * DAY, phone=r.choice(phones), address=r.choice(addrs))
        a = w.account(p, opened + r.randint(0, 6) * DAY, balance=r.uniform(100, 800))
        w.used_device.append((a, dev))
        w.accessed_from.append((a, w.ip("residential")))
        accs.append(a)
    sink_p = w.person(opened)
    sink = w.account(sink_p, opened)
    w.used_device.append((sink, dev))
    for a in accs:  # bust-out: everything leaves on the same day
        burst = END - r.randint(2, 6) * DAY
        w.transfer(a, sink, r.uniform(40_000, 95_000), burst + r.randint(0, 6 * 3600), "IMPS")
    w.pay(sink, w.merchant_of("money_transfer"), r.uniform(200_000, 400_000), END - DAY)
    w.truth.append({"ring": f"SYNTH-{idx}", "typology": "synthetic_identity_bust_out", "accounts": accs + [sink]})


def generate(seed: int = 7, n_people: int = 2400) -> World:
    w = World(seed=seed, rng=random.Random(seed))
    customers = _normal_population(w, n_people)
    _benign_lookalikes(w, customers)
    for i in range(4):
        _mule_network(w, customers, i + 1)
    for i in range(2):
        _round_trip(w, i + 1)
    for i in range(2):
        _synthetic_identity(w, i + 1)
    w.transfers.sort(key=lambda t: t["ts"])
    return w


def save_truth(w: World, path: str):
    with open(path, "w") as f:
        json.dump(w.truth, f, indent=2)
