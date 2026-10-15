"""Conservative, deterministic technology *clues*, not proficiency assessment.

``match_requirement(jd_clause, original_resume)`` returns JSON-compatible data.
Offsets are zero-based Python Unicode indices, end-exclusive, into the UNMODIFIED
resume. ``quote`` is the exact matched surface; ``context`` has its own offsets.
``matched`` means every Boolean group has a positive practice clue, never that
an applicant meets a level, duration, responsibility or production requirement.

Extend CONCEPT_ALIASES and DIRECTED_RELATIONS rather than adding language-specific
branches. Relationships are ONE HOP and directional. Weak/context relationships
are visible but never satisfy a group. Unknown ASCII tokens from the requirement
are matched literally, with the same boundary and negation rules as known terms.
No model, filesystem, environment, network or third-party dependencies are used.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


# The canonical spelling is always an accepted surface too.
CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": ("Python3", "Python 3"),
    "Java": (), "JavaScript": ("JS", "ECMAScript"), "TypeScript": ("TS",),
    "C": (), "C++": ("CPP",), "C#": ("CSharp", "C Sharp"),
    "Go": ("Golang",), "R": (), "Rust": (), "Ruby": (), "PHP": (),
    "Kotlin": (), "Swift": (), "Scala": (), "Dart": (), "Lua": (),
    "SQL": (), "HTML": ("HTML5",), "CSS": ("CSS3",),
    "Flask": (), "FastAPI": (), "Django": (),
    "Spring": ("Spring Framework",), "Spring Boot": ("SpringBoot",),
    "NestJS": ("Nest.js", "Nest JS"), "Node.js": ("NodeJS", "Node JS"),
    "React": ("ReactJS", "React.js"), "Vue": ("Vue.js", "VueJS", "Vue3"),
    "Angular": (), "Next.js": ("NextJS",), "Nuxt": ("Nuxt.js",),
    ".NET": ("DotNet", ".NET Core"), "ASP.NET": ("ASP.NET Core",), "Express": ("Express.js",),
    "Rails": ("Ruby on Rails",), "Laravel": (), "Gin": (),
    "PostgreSQL": ("Postgres", "PGSQL"), "MySQL": (), "SQLite": (),
    "SQL Server": ("MSSQL", "Microsoft SQL Server"), "Oracle": (),
    "MongoDB": ("Mongo",), "Redis": (), "Elasticsearch": ("ElasticSearch", "ES"),
    "ClickHouse": (), "Milvus": (), "Qdrant": (), "Chroma": ("ChromaDB",),
    "AWS": ("Amazon Web Services", "亚马逊云"),
    "Azure": ("Microsoft Azure", "微软云"),
    "GCP": ("Google Cloud", "Google Cloud Platform", "谷歌云"),
    "Alibaba Cloud": ("Aliyun", "阿里云"), "Tencent Cloud": ("腾讯云",),
    "Kubernetes": ("K8s",), "Docker": (), "DevOps": (),
    "Terraform": (), "Ansible": (), "Jenkins": (), "Git": (),
    "Linux": (), "Nginx": (), "Kafka": (), "RabbitMQ": (),
    "Spark": ("Apache Spark",), "Hadoop": (), "Flink": (),
    "PyTorch": (), "TensorFlow": (), "scikit-learn": ("sklearn",),
    "Pandas": (), "NumPy": (),
    "LLM": ("大语言模型", "大型语言模型", "Large Language Model"),
    "RAG": ("检索增强生成", "检索增强", "Retrieval Augmented Generation", "Retrieval-Augmented Generation"),
    "Agent": ("AI Agent", "智能体", "AI智能体"),
    "LangChain": (), "LangGraph": (), "LlamaIndex": (), "AutoGen": (),
    "CrewAI": (), "MCP": ("Model Context Protocol",),
    "Prompt Engineering": ("提示词工程",), "Fine-tuning": ("微调", "Fine tuning", "Finetuning"),
}


@dataclass(frozen=True)
class TechnologyRelation:
    source: str
    target: str
    strength: str  # practice: useful single-hop clue; context: cannot fill a gap
    reason: str


DIRECTED_RELATIONS: tuple[TechnologyRelation, ...] = tuple(
    TechnologyRelation(source, target, "practice", "framework_or_library_practice")
    for target, sources in {
        "Python": ("Flask", "FastAPI", "Django", "Pandas", "NumPy", "scikit-learn"),
        "Java": ("Spring", "Spring Boot"),
        "Ruby": ("Rails",), "PHP": ("Laravel",), "Go": ("Gin",),
        "SQL": ("PostgreSQL", "MySQL", "SQLite", "SQL Server"),
    }.items() for source in sources
) + tuple(
    TechnologyRelation(source, target, "context", reason)
    for source, target, reason in (
        ("NestJS", "TypeScript", "framework_allows_multiple_languages"),
        ("NestJS", "JavaScript", "framework_allows_multiple_languages"),
        ("Node.js", "JavaScript", "runtime_allows_multiple_languages"),
        ("Docker", "DevOps", "tool_does_not_establish_broad_competence"),
        ("Kubernetes", "DevOps", "tool_does_not_establish_broad_competence"),
        ("LangChain", "Agent", "library_does_not_establish_agent_implementation"),
        ("LangGraph", "Agent", "library_does_not_establish_agent_implementation"),
        ("LlamaIndex", "RAG", "library_does_not_establish_rag_implementation"),
    )
)

# ASCII wording which is grammatical/qualification metadata, not a technology.
# Unknown *technical* tokens remain an open set; no dictionary membership gate.
_NON_TECH = frozenset("and or with in of the a an to for on using use used experience experienced knowledge proficient proficiency familiar familiarity skilled skills skill expertise expert advanced basic intermediate required preferred must have years year months month development developer developing programming language languages framework frameworks database databases cloud tools tool technology technologies strong good excellent ability working work solid understanding communication teamwork responsibility at least any one either both plus not only engineering senior junior engineer version v etc e.g i.e".split())
_TOKEN = re.compile(r"(?<![A-Za-z0-9_+#])(?:\.[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*|[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*(?:\+\+|#)?)(?![A-Za-z0-9_+#])")
_BREAK = re.compile(r"[。；;！!？?\n\r，,]|但是|不过|然而|但|\bbut\b|\bhowever\b", re.I)
_NEG = re.compile(r"(?:从未|尚未|未曾|未体现|未提供|未涉及|没有|并未|未能|不会|不懂|不熟悉|不熟练|不掌握|不具备|不擅长|不使用|不用|未接触|未使用|未用过|未掌握|未熟悉|未学过|未学习|无(?:相关)?|缺乏)|\b(?:no|not|never|without|lack(?:s|ing)?|unfamiliar)\b|\b(?:haven't|hasn't|didn't|don't|doesn't|cannot|can't)\b", re.I)
_PSEUDO_NEG = re.compile(r"不仅|不但|不只是|不止|\bnot\s+only\b|\bno\s+less\s+than\b", re.I)
_POSITIVE_RESET = re.compile(r"(?:熟悉|掌握|使用|用过|擅长|精通|了解)|\b(?:use[ds]?|know|proficient|familiar|experienced)\b", re.I)


def _pattern(surface: str) -> re.Pattern:
    # ASCII boundaries deliberately include +/#: C must not eat C++/C#;
    # Java must not eat JavaScript. Chinese adjacent to ASCII is allowed.
    escaped = re.escape(surface).replace(r"\ ", r"\s+")
    # Long technology names commonly carry a numeric version (React19/Next.js15).
    suffix = r"(?![A-Za-z_+#])" if len(surface) >= 3 else r"(?![A-Za-z0-9_+#])"
    return re.compile(r"(?<![A-Za-z0-9_+#.])" + escaped + suffix, re.I) if surface[0].isascii() else re.compile(escaped, re.I)


def _mentions(text: str, extra: dict[str, str] | None = None) -> list[dict]:
    candidates = []
    for concept, aliases in CONCEPT_ALIASES.items():
        for surface in (concept,) + aliases:
            for found in _pattern(surface).finditer(text):
                # Standalone R is too ambiguous when lower-case in ordinary prose.
                if concept == "R" and found.group() != "R":
                    continue
                candidates.append({"concept": concept, "start": found.start(), "end": found.end(), "quote": found.group()})
    for concept, surface in (extra or {}).items():
        for found in _pattern(surface).finditer(text):
            candidates.append({"concept": concept, "start": found.start(), "end": found.end(), "quote": found.group()})
    # Longest surface wins at an overlapping span (Spring Boot, AI Agent, C++).
    selected = []
    for item in sorted(candidates, key=lambda m: (-(m["end"] - m["start"]), m["start"], m["concept"])):
        if not any(item["start"] < old["end"] and old["start"] < item["end"] for old in selected):
            selected.append(item)
    return sorted(selected, key=lambda m: m["start"])


def _requirement_mentions(text: str) -> tuple[list[dict], dict[str, str]]:
    known = _mentions(text)
    extra = {}
    for found in _TOKEN.finditer(text):
        if any(found.start() < item["end"] and item["start"] < found.end() for item in known):
            continue
        token = found.group()
        if token.casefold() in _NON_TECH or re.fullmatch(r"v?\d+(?:\.\d+)*", token, re.I):
            continue
        # Lower-case one-letter words are not useful technical evidence.
        if len(token) == 1 and not token.isupper():
            continue
        extra.setdefault(token.casefold(), token)
    return _mentions(text, extra), extra


def _groups(text: str, mentions: list[dict]) -> list[list[str]]:
    """CNF: AND of OR groups. Slash/或/or are alternatives; all other gaps AND.

    Parentheses are respected. Without parentheses alternatives bind tighter:
    Python/Java 和 SQL => (Python OR Java) AND SQL. Use parentheses to express
    alternatives between bundles: (Python 和 SQL) 或 (Java 和 Redis).
    """
    tokens = []
    previous_end = 0
    for item in mentions:
        gap = text[previous_end:item["start"]]
        for op in re.finditer(r"[（()）]|/|\||或者|或是|或|\bor\b", gap, re.I):
            value = op.group()
            tokens.append("(" if value in ("(", "（") else ")" if value in (")", "）") else "OR")
        tokens.append([ [item["concept"]] ])
        previous_end = item["end"]
    tokens.extend(")" for ch in text[previous_end:] if ch in ")）")
    # Adjacent atoms/parentheses are implicit conjunctions. OR combines CNFs by
    # distribution, AND concatenates; bounded to avoid pathological JD expansion.
    pos = 0

    def atom():
        nonlocal pos
        if pos >= len(tokens):
            return []
        token = tokens[pos]
        pos += 1
        if token == "(":
            value = expression()
            if pos < len(tokens) and tokens[pos] == ")":
                pos += 1
            return value
        return token if isinstance(token, list) else []

    def alternative():
        nonlocal pos
        value = atom()
        while pos < len(tokens) and tokens[pos] == "OR":
            pos += 1
            other = atom()
            if value and other:
                if len(value) * len(other) > 256:
                    # Conservative fallback: require both bundles, never pass
                    # an oversized expression by silently dropping constraints.
                    value += other
                else:
                    value = [list(dict.fromkeys(a + b)) for a in value for b in other]
            else:
                value = value or other
        return value

    def expression():
        value = []
        while pos < len(tokens) and tokens[pos] != ")":
            value += alternative()
        return value

    result = expression()
    # Explicit list-wide choice markers: “Python、Java任一种” / “one of ...”.
    # Only apply to a flat list; nested bundles retain their own Boolean scope.
    if not re.search(r"[（()）]", text) and re.search(
        r"任选|任一|至少一(?:种|项|门|个)|其中一(?:种|项|门|个)|一种即可|\b(?:one|any)\s+of\b", text, re.I
    ):
        result = [list(dict.fromkeys(m["concept"] for m in mentions))]
    return [list(group) for group in dict.fromkeys(tuple(g) for g in result)]


def _context(text: str, mention: dict) -> tuple[int, int]:
    start, end = 0, len(text)
    for boundary in _BREAK.finditer(text):
        if boundary.end() <= mention["start"]:
            start = boundary.end()
        elif boundary.start() >= mention["end"]:
            end = boundary.start()
            break
    return start, end


def _negative(text: str, item: dict, all_mentions: list[dict], start: int, end: int) -> bool:
    prefix = text[start:item["start"]]
    cleaned = _PSEUDO_NEG.sub(lambda m: " " * len(m.group()), prefix)
    negatives = list(_NEG.finditer(cleaned))
    if negatives:
        last = negatives[-1]
        # "不熟悉Python和Java" keeps scope; "不会Python而熟悉Java" resets
        # only if a new positive predicate follows another technology mention.
        earlier = [m for m in all_mentions if start <= m["start"] < item["start"]]
        reset = bool(earlier and _POSITIVE_RESET.search(cleaned[max(last.end(), earlier[-1]["end"] - start):]))
        if not reset:
            return True
    following = min((m["start"] for m in all_mentions if item["end"] <= m["start"] < end), default=end)
    suffix = _PSEUDO_NEG.sub("", text[item["end"]:following])
    # Postfix negation applies to the preceding technology, not the next one.
    return bool(re.match(r"\s*(?:经验|技能|能力|experience|skills)?\s*(?:[:：]\s*)?(?:不熟悉|不熟练|不会|不懂|未掌握|未接触|无经验|缺乏|不足|\bis\s+not\b|\bnot\b|\bnone\b)", suffix, re.I))


def match_requirement(requirement_text: str, resume_content: str) -> dict:
    """Return clues, exact citations, missing concepts and explicit negatives.

    ``matches`` may contain context-only relations; check ``satisfies`` rather
    than its length. ``missing_concepts`` lists absent alternatives only in
    UNSATISFIED groups (Python OR Java with Java found has no missing concept).
    ``explicit_negative`` includes only literal/alias negatives, never infers a
    target's absence from a negative source framework. A positive and explicit
    negative for the same concept set ``conflicting_concepts`` and block its
    satisfaction pending human review. Empty/nontechnical input never matches.
    """
    requirement = str(requirement_text or "")
    resume = str(resume_content or "")
    requested, extra = _requirement_mentions(requirement)
    concepts = list(dict.fromkeys(item["concept"] for item in requested))
    groups = _groups(requirement, requested)
    mentions = _mentions(resume, extra)
    matches, negatives = [], []
    for item in mentions:
        start, end = _context(resume, item)
        negative = _negative(resume, item, mentions, start, end)
        for target in concepts:
            relation = next((r for r in DIRECTED_RELATIONS if r.source == item["concept"] and r.target == target), None)
            same = target == item["concept"]
            if not same and relation is None:
                continue
            exact = item["quote"].casefold() == target.casefold()
            clue = {
                "concept": target, "source_concept": item["concept"],
                "match_type": ("direct" if exact else "alias") if same else "related",
                "strength": "direct" if same else relation.strength,
                "quote": item["quote"], "start": item["start"], "end": item["end"],
                "context": resume[start:end], "context_start": start, "context_end": end,
                "satisfies": same or relation.strength == "practice",
                "reason": "literal_mention" if same else relation.reason,
            }
            if negative:
                if same:
                    clue["satisfies"] = False
                    negatives.append(clue)
            else:
                matches.append(clue)
    positive = {m["concept"] for m in matches if m["satisfies"]}
    negative_concepts = {m["concept"] for m in negatives}
    conflicts = positive & negative_concepts
    available = positive - negative_concepts
    for clue in matches:
        if clue["concept"] in negative_concepts:
            clue["satisfies"] = False
    missing_groups = [g for g in groups if not available.intersection(g)]
    missing = list(dict.fromkeys(c for g in missing_groups for c in g))
    return {
        "has_technology": bool(concepts), "concepts": concepts, "groups": groups,
        "matches": matches, "missing_concepts": missing, "missing_groups": missing_groups,
        "explicit_negative": negatives,
        "conflicting_concepts": [c for c in concepts if c in conflicts],
        "matched": bool(groups) and not missing_groups,
        "evidence_scope": "technology_practice_clues_only",
    }
