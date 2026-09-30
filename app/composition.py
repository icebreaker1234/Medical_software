"""Medicine composition: parse, normalise and compare formulas.

Two products are EXACT substitutes only when all of these match:
  * the same active ingredient(s)          e.g. Aceclofenac + Paracetamol
  * the same strength of EACH ingredient   e.g. 100 mg + 325 mg
  * the same dosage form                   tablet != syrup != inhaler
  * the same release type                  plain (IR) != SR / ER / DR

The key for that is `comp_key`, e.g.
    "aceclofenac 100mg + paracetamol 325mg | tablet | IR"

Composition data for the demo catalogue and brand master is ILLUSTRATIVE -
a real store must load a verified drug master before relying on it.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
BRAND_MASTER = DATA_DIR / "brand_master.csv"

# ---------------------------------------------------------------- vocabulary
SALT_ALIASES = {
    "acetaminophen": "paracetamol", "acetylsalicylic acid": "aspirin", "albuterol": "salbutamol",
    "vitamin d3": "cholecalciferol", "cetirizine dihydrochloride": "cetirizine",
    "cetirizine hydrochloride": "cetirizine", "levocetirizine dihydrochloride": "levocetirizine",
    "levocetirizine hydrochloride": "levocetirizine", "montelukast sodium": "montelukast",
    "pantoprazole sodium": "pantoprazole", "amlodipine besylate": "amlodipine",
    "amlodipine besilate": "amlodipine", "atorvastatin calcium": "atorvastatin",
    "metformin hydrochloride": "metformin", "metformin hcl": "metformin",
    "fexofenadine hydrochloride": "fexofenadine", "cefpodoxime proxetil": "cefpodoxime",
    "levothyroxine sodium": "levothyroxine", "thyroxine sodium": "levothyroxine",
    "hydroxyzine hydrochloride": "hydroxyzine", "zolpidem tartrate": "zolpidem",
    "formoterol fumarate": "formoterol", "salbutamol sulphate": "salbutamol",
    "salbutamol sulfate": "salbutamol", "chlorpheniramine maleate": "chlorpheniramine",
    "ondansetron hydrochloride": "ondansetron", "rabeprazole sodium": "rabeprazole",
    "clavulanate potassium": "clavulanic acid", "potassium clavulanate": "clavulanic acid",
    "dicyclomine hydrochloride": "dicyclomine", "azithromycin dihydrate": "azithromycin",
    "amoxicillin trihydrate": "amoxicillin", "amoxycillin": "amoxicillin",
}
# note: diclofenac SODIUM and diclofenac POTASSIUM are deliberately NOT merged

FORMS = [  # (regex on the product / brand name, canonical form) - first match wins
    (r"\bdt\b|dispersible", "dispersible tablet"),
    (r"\binhaler\b|\brotacap|\bmdi\b", "inhaler"),
    (r"\bsyrup\b|\bsyp\b", "syrup"),
    (r"\bsuspension\b|\bsusp\b", "suspension"),
    (r"\bdrops?\b", "drops"),
    (r"\bgel\b|\bemulgel\b", "gel"),
    (r"\bcream\b", "cream"),
    (r"\bointment\b|\boint\b", "ointment"),
    (r"\binj\b|\binjection\b", "injection"),
    (r"\bsachet\b", "sachet"),
    (r"\bcap\b|\bcaps\b|\bcapsule", "capsule"),
    (r"\btab\b|\btabs\b|\btablet", "tablet"),
]
RELEASES = [(r"\bsr\b|sustained", "SR"), (r"\ber\b|\bxr\b|\bxl\b|extended", "ER"),
            (r"\bcr\b|controlled", "CR"), (r"\bmr\b|modified", "MR"),
            (r"\bdr\b|\bec\b|delayed|enteric|gastro.?resistant", "DR")]

# Narrow-therapeutic-index salts: a brand switch needs extra care / prescriber consent.
NTI_SALTS = {"levothyroxine", "warfarin", "phenytoin", "carbamazepine", "lithium", "digoxin",
             "theophylline", "cyclosporine", "tacrolimus", "valproate", "sodium valproate",
             "valproic acid", "phenobarbital"}

# ---------------------------------------------------------------- demo catalogue compositions
# product name -> (composition with strength, dosage form, release)
CATALOGUE_COMPOSITION = {
    "Diclofenac 50mg Tab": ("Diclofenac Sodium 50mg", "tablet", "IR"),
    "Aceclofenac 100mg Tab": ("Aceclofenac 100mg", "tablet", "IR"),
    "Aceclofenac + Paracetamol Tab": ("Aceclofenac 100mg + Paracetamol 325mg", "tablet", "IR"),
    "Diclofenac Gel 30g": ("Diclofenac Diethylamine 1.16%", "gel", "IR"),
    "Ibuprofen 400mg Tab": ("Ibuprofen 400mg", "tablet", "IR"),
    "Ibuprofen + Paracetamol Tab": ("Ibuprofen 400mg + Paracetamol 325mg", "tablet", "IR"),
    "Naproxen 250mg Tab": ("Naproxen 250mg", "tablet", "IR"),
    "Aspirin 75mg Tab": ("Aspirin 75mg", "tablet", "IR"),
    "Aspirin 150mg Tab": ("Aspirin 150mg", "tablet", "IR"),
    "Paracetamol 500mg Tab": ("Paracetamol 500mg", "tablet", "IR"),
    "Paracetamol 650mg Tab": ("Paracetamol 650mg", "tablet", "IR"),
    "Paracetamol Syrup 60ml": ("Paracetamol 125mg/5ml", "syrup", "IR"),
    "Alprazolam 0.25mg Tab": ("Alprazolam 0.25mg", "tablet", "IR"),
    "Alprazolam 0.5mg Tab": ("Alprazolam 0.5mg", "tablet", "IR"),
    "Clonazepam 0.5mg Tab": ("Clonazepam 0.5mg", "tablet", "IR"),
    "Hydroxyzine 25mg Tab": ("Hydroxyzine 25mg", "tablet", "IR"),
    "Zolpidem 10mg Tab": ("Zolpidem 10mg", "tablet", "IR"),
    "Melatonin 3mg Tab": ("Melatonin 3mg", "tablet", "IR"),
    "Salbutamol Inhaler 100mcg": ("Salbutamol 100mcg", "inhaler", "IR"),
    "Montelukast + Levocetirizine Tab": ("Montelukast 10mg + Levocetirizine 5mg", "tablet", "IR"),
    "Budesonide + Formoterol Inhaler": ("Budesonide 200mcg + Formoterol 6mcg", "inhaler", "IR"),
    "Etofylline + Theophylline Tab": ("Etofylline 77mg + Theophylline 23mg", "tablet", "IR"),
    "Cetirizine 10mg Tab": ("Cetirizine 10mg", "tablet", "IR"),
    "Levocetirizine 5mg Tab": ("Levocetirizine 5mg", "tablet", "IR"),
    "Fexofenadine 120mg Tab": ("Fexofenadine 120mg", "tablet", "IR"),
    "Chlorpheniramine Syrup 100ml": ("Chlorpheniramine 2mg/5ml", "syrup", "IR"),
    "Amoxicillin 500mg Cap": ("Amoxicillin 500mg", "capsule", "IR"),
    "Azithromycin 500mg Tab": ("Azithromycin 500mg", "tablet", "IR"),
    "Cefpodoxime 200mg Tab": ("Cefpodoxime 200mg", "tablet", "IR"),
    "Pantoprazole 40mg Tab": ("Pantoprazole 40mg", "tablet", "DR"),
    "Antacid Suspension 170ml": ("Magaldrate 400mg/5ml + Simethicone 20mg/5ml", "suspension", "IR"),
    "Metformin 500mg Tab": ("Metformin 500mg", "tablet", "IR"),
    "Glimepiride 1mg Tab": ("Glimepiride 1mg", "tablet", "IR"),
    "Amlodipine 5mg Tab": ("Amlodipine 5mg", "tablet", "IR"),
    "Telmisartan 40mg Tab": ("Telmisartan 40mg", "tablet", "IR"),
    "Atorvastatin 10mg Tab": ("Atorvastatin 10mg", "tablet", "IR"),
    "Vitamin D3 60000 IU Cap": ("Cholecalciferol 60000IU", "capsule", "IR"),
    "Calcium + D3 Tab": ("Calcium Carbonate 1250mg + Cholecalciferol 250IU", "tablet", "IR"),
    # alternate brands from other (fictional) manufacturers - same formula, different name
    "Vedant Paracetamol 650 Tab": ("Paracetamol 650mg", "tablet", "IR"),
    "Kiran Aceclo-P Tab": ("Aceclofenac 100mg + Paracetamol 325mg", "tablet", "IR"),
    "Vedant Ibu-Para Tab": ("Ibuprofen 400mg + Paracetamol 325mg", "tablet", "IR"),
    "Kiran Montelukast-LC Tab": ("Montelukast 10mg + Levocetirizine 5mg", "tablet", "IR"),
    "Arogya Pantoprazole 40 Tab": ("Pantoprazole 40mg", "tablet", "DR"),
    "Sanjeevani Amlodipine 5 Tab": ("Amlodipine 5mg", "tablet", "IR"),
    "Vedant Cetirizine 10 Tab": ("Cetirizine 10mg", "tablet", "IR"),
    "Kiran Azithro 500 Tab": ("Azithromycin 500mg", "tablet", "IR"),
    "Arogya Metformin SR 500 Tab": ("Metformin 500mg", "tablet", "SR"),
    "Sanjeevani Alprazolam 0.25 Tab": ("Alprazolam 0.25mg", "tablet", "IR"),
    "Vedant Theo-Eto Tab": ("Etofylline 77mg + Theophylline 23mg", "tablet", "IR"),
}


# ---------------------------------------------------------------- parsing
_STRENGTH = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|ug|gm|g|iu|%)(?:\s*/\s*(\d+(?:\.\d+)?)?\s*(ml))?", re.I)


@dataclass(frozen=True)
class Ingredient:
    salt: str
    strength: str          # canonical, e.g. "650mg", "25mg/ml", "60000iu", "1.16%"

    def __str__(self) -> str:
        return f"{self.salt} {self.strength}".strip()


def norm_salt(s: str) -> str:
    s = re.sub(r"\s+", " ", s.lower().replace("-", " ")).strip(" .,")
    return SALT_ALIASES.get(s, s)


def norm_strength(num: str, unit: str, per: str | None = None, per_unit: str | None = None) -> str:
    v, u = float(num), unit.lower()
    if u in ("g", "gm"):
        v, u = v * 1000, "mg"
    elif u in ("µg", "ug"):
        u = "mcg"
    elif u == "mcg" and v >= 1000:
        v, u = v / 1000, "mg"
    if per_unit:                                   # 125mg/5ml -> 25mg/ml
        v = v / float(per or 1)
        return f"{v:g}{u}/ml"
    return f"{v:g}{u}"


def parse_composition(text: str) -> tuple[Ingredient, ...]:
    """'Aceclofenac 100mg + Paracetamol 325 mg' -> sorted ingredients."""
    parts = re.split(r"\s*(?:\+|/(?!\s*\d*\s*ml)|,| and )\s*", text or "", flags=re.I)
    out = []
    for part in parts:
        if not part.strip():
            continue
        m = _STRENGTH.search(part)
        if m:
            salt = norm_salt(part[:m.start()])
            strength = norm_strength(*m.groups())
        else:
            salt, strength = norm_salt(part), ""
        if salt:
            out.append(Ingredient(salt, strength))
    return tuple(sorted(out, key=lambda i: i.salt))


def detect_form(name: str) -> str | None:
    n = name.lower()
    return next((form for pat, form in FORMS if re.search(pat, n)), None)


def detect_release(name: str) -> str:
    n = name.lower()
    return next((rel for pat, rel in RELEASES if re.search(pat, n)), "IR")


def make_key(ingredients: tuple[Ingredient, ...], form: str | None, release: str = "IR") -> str | None:
    """Exact-substitute key. None when anything needed for a safe match is missing."""
    if not ingredients or not form or any(not i.strength for i in ingredients):
        return None
    return f"{' + '.join(map(str, ingredients))} | {form} | {release}"


def salts_key(ingredients: tuple[Ingredient, ...]) -> str:
    """Looser key: same active ingredients, any strength / form (for 'near matches')."""
    return " + ".join(i.salt for i in ingredients)


def describe(composition: str, form: str | None, release: str = "IR") -> dict:
    ing = parse_composition(composition)
    return {"composition": composition, "dosage_form": form, "release_type": release or "IR",
            "comp_key": make_key(ing, form, release or "IR"), "salts": salts_key(ing),
            "ingredients": ing}


def infer_from_product(name: str, generic: str | None) -> dict | None:
    """Best-effort composition for a product added without one (single-salt only).

    'Paracetamol 650mg Tab' + generic 'Paracetamol' -> Paracetamol 650mg | tablet | IR.
    Combinations are left unmatched on purpose - guessing strengths is unsafe.
    """
    if name in CATALOGUE_COMPOSITION:
        return describe(*CATALOGUE_COMPOSITION[name])
    generic = (generic or "").strip()
    form = detect_form(name)
    if not generic or not form:
        return None
    if _STRENGTH.search(generic):                  # generic already has strengths
        return describe(generic, form, detect_release(name))
    if re.search(r"[+/,]", generic):
        return None
    strengths = _STRENGTH.findall(name)
    if len(strengths) != 1:
        return None
    num, unit, per, per_unit = strengths[0]
    comp = f"{generic} {num}{unit}" + (f"/{per}{per_unit}" if per_unit else "")
    return describe(comp, form, detect_release(name))


def nti_salts(ingredients) -> list[str]:
    return sorted({i.salt for i in ingredients} & NTI_SALTS)


# ---------------------------------------------------------------- brand master
def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9. ]+", " ", s.lower()).strip()


def load_brand_master(extra: Path | None = None) -> list[dict]:
    """Brand -> composition list. Built-in demo file + an optional store-uploaded file.

    Accepted columns: brand, composition, form, release  (our format), or the Kaggle
    "A-Z Medicine Dataset of India" format: name, short_composition1, short_composition2.
    """
    rows: list[dict] = []
    for path in (BRAND_MASTER, extra):
        if not path or not Path(path).exists():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                brand = (r.get("brand") or r.get("name") or "").strip()
                comp = (r.get("composition") or " + ".join(
                    c.strip() for c in (r.get("short_composition1"), r.get("short_composition2"))
                    if c and c.strip())).strip()
                if not brand or not comp:
                    continue
                form = (r.get("form") or "").strip() or detect_form(brand) or "tablet"
                release = (r.get("release") or "").strip() or detect_release(brand)
                d = describe(comp, form, release)
                d.update(brand=brand, norm=_norm_name(brand))
                rows.append(d)
    return rows


def lookup_brand(query: str, master: list[dict]) -> dict | None:
    """Find a brand by what the customer / pharmacist typed ('dolo 650', 'montair lc')."""
    q = _norm_name(query)
    if not q:
        return None
    toks = q.split()
    exact = [m for m in master if m["norm"] == q]
    if exact:
        return exact[0]
    hits = [m for m in master if all(t in m["norm"].split() or t in m["norm"] for t in toks)]
    if hits:
        return min(hits, key=lambda m: len(m["norm"]))       # most specific match
    import difflib
    close = difflib.get_close_matches(q, [m["norm"] for m in master], n=1, cutoff=0.82)
    return next((m for m in master if close and m["norm"] == close[0]), None)
