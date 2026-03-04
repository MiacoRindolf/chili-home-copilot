"""Clone data-collection service: question bank, answer persistence, progress tracking."""
import json
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CloneProfile, CloneQA

# ---------------------------------------------------------------------------
# Question bank — each question has a unique key, category, prompt,
# options (id + label), and whether multi-select is allowed.
# ---------------------------------------------------------------------------

QUESTION_BANK: list[dict] = [
    # ── Identity & Values ──────────────────────────────────────────
    {
        "key": "core_values",
        "category": "Identity & Values",
        "question": "What values matter most to you in life?",
        "allow_multiple": True,
        "options": [
            {"id": "honesty", "label": "Honesty & transparency"},
            {"id": "loyalty", "label": "Loyalty & commitment"},
            {"id": "growth", "label": "Personal growth & learning"},
            {"id": "freedom", "label": "Independence & freedom"},
            {"id": "family", "label": "Family & relationships"},
            {"id": "creativity", "label": "Creativity & self-expression"},
            {"id": "security", "label": "Stability & security"},
            {"id": "impact", "label": "Making an impact on the world"},
            {"id": "faith", "label": "Faith & spirituality"},
            {"id": "fun", "label": "Fun & enjoying life"},
        ],
    },
    {
        "key": "life_priority",
        "category": "Identity & Values",
        "question": "If you had to rank these, what's your #1 priority right now?",
        "allow_multiple": False,
        "options": [
            {"id": "career", "label": "Career & professional growth"},
            {"id": "health", "label": "Health & fitness"},
            {"id": "relationships", "label": "Relationships & social life"},
            {"id": "wealth", "label": "Financial independence"},
            {"id": "learning", "label": "Learning & skills"},
            {"id": "peace", "label": "Mental peace & balance"},
        ],
    },
    {
        "key": "non_negotiable",
        "category": "Identity & Values",
        "question": "What's something you would never compromise on?",
        "allow_multiple": True,
        "options": [
            {"id": "integrity", "label": "My integrity / word"},
            {"id": "family_time", "label": "Time with family"},
            {"id": "health", "label": "My health"},
            {"id": "beliefs", "label": "My core beliefs"},
            {"id": "independence", "label": "My independence"},
            {"id": "quality", "label": "Quality of my work"},
        ],
    },

    # ── Decision Making ────────────────────────────────────────────
    {
        "key": "decision_style",
        "category": "Decision Making",
        "question": "How do you typically make important decisions?",
        "allow_multiple": False,
        "options": [
            {"id": "gut", "label": "Trust my gut instinct"},
            {"id": "research", "label": "Research thoroughly, then decide"},
            {"id": "consult", "label": "Ask people I trust for input"},
            {"id": "pros_cons", "label": "Write out pros and cons"},
            {"id": "sleep_on_it", "label": "Sleep on it — let it settle"},
            {"id": "act_fast", "label": "Decide quickly, adjust later"},
        ],
    },
    {
        "key": "risk_tolerance",
        "category": "Decision Making",
        "question": "How do you feel about taking risks?",
        "allow_multiple": False,
        "options": [
            {"id": "love_risk", "label": "I thrive on risk — high risk, high reward"},
            {"id": "calculated", "label": "Calculated risks only — I need good odds"},
            {"id": "cautious", "label": "I prefer the safe path unless there's a strong reason"},
            {"id": "avoid", "label": "I avoid risk whenever possible"},
            {"id": "depends", "label": "Depends entirely on the domain"},
        ],
    },
    {
        "key": "regret_handling",
        "category": "Decision Making",
        "question": "When a decision turns out badly, what's your typical response?",
        "allow_multiple": False,
        "options": [
            {"id": "learn", "label": "Analyze what went wrong and learn from it"},
            {"id": "move_on", "label": "Accept it quickly and move on"},
            {"id": "dwell", "label": "I tend to dwell on it for a while"},
            {"id": "fix", "label": "Immediately focus on fixing the situation"},
            {"id": "reframe", "label": "Reframe it — everything happens for a reason"},
        ],
    },
    {
        "key": "time_vs_money",
        "category": "Decision Making",
        "question": "When you have to choose between saving time or saving money, you usually...",
        "allow_multiple": False,
        "options": [
            {"id": "time", "label": "Pay more to save time — time is priceless"},
            {"id": "money", "label": "Spend time to save money — I'm resourceful"},
            {"id": "balance", "label": "Depends on the amount — I weigh each case"},
            {"id": "time_career", "label": "Save time on work stuff, save money on personal stuff"},
        ],
    },

    # ── Communication Style ────────────────────────────────────────
    {
        "key": "comm_style",
        "category": "Communication",
        "question": "How would you describe your communication style?",
        "allow_multiple": True,
        "options": [
            {"id": "direct", "label": "Direct & to the point"},
            {"id": "diplomatic", "label": "Diplomatic & considerate"},
            {"id": "humorous", "label": "Humorous & lighthearted"},
            {"id": "analytical", "label": "Analytical & detail-oriented"},
            {"id": "empathetic", "label": "Warm & empathetic"},
            {"id": "concise", "label": "Short & concise messages"},
            {"id": "verbose", "label": "Thorough — I explain everything"},
        ],
    },
    {
        "key": "conflict_approach",
        "category": "Communication",
        "question": "When there's a conflict or disagreement, you tend to...",
        "allow_multiple": False,
        "options": [
            {"id": "confront", "label": "Address it head-on right away"},
            {"id": "cool_down", "label": "Take time to cool down first"},
            {"id": "compromise", "label": "Look for a compromise immediately"},
            {"id": "avoid", "label": "Avoid it unless it's really important"},
            {"id": "listen", "label": "Listen to their side first, then respond"},
        ],
    },
    {
        "key": "feedback_preference",
        "category": "Communication",
        "question": "How do you prefer to receive feedback?",
        "allow_multiple": False,
        "options": [
            {"id": "blunt", "label": "Give it to me straight — no sugarcoating"},
            {"id": "sandwich", "label": "Start with what's good, then the issue"},
            {"id": "private", "label": "Privately, one-on-one"},
            {"id": "written", "label": "In writing so I can process it"},
            {"id": "examples", "label": "With specific examples, not generalities"},
        ],
    },

    # ── Financial ──────────────────────────────────────────────────
    {
        "key": "spending_style",
        "category": "Financial",
        "question": "What best describes your relationship with money?",
        "allow_multiple": False,
        "options": [
            {"id": "saver", "label": "I'm a natural saver — I track everything"},
            {"id": "spender", "label": "I enjoy spending on things that matter to me"},
            {"id": "investor", "label": "I focus on growing wealth — investing over saving"},
            {"id": "generous", "label": "I'm generous — I spend freely on others"},
            {"id": "minimal", "label": "Minimalist — I don't need much"},
            {"id": "anxious", "label": "Money stresses me out — I try not to think about it"},
        ],
    },
    {
        "key": "big_purchase",
        "category": "Financial",
        "question": "Before a big purchase (>$200), you typically...",
        "allow_multiple": True,
        "options": [
            {"id": "research", "label": "Research extensively — reviews, comparisons"},
            {"id": "impulse", "label": "If I want it, I buy it"},
            {"id": "wait", "label": "Wait a few days to see if I still want it"},
            {"id": "budget", "label": "Check if it fits my budget first"},
            {"id": "ask", "label": "Ask someone I trust for their opinion"},
            {"id": "quality", "label": "Always pay more for quality"},
        ],
    },

    # ── Social & Relationships ─────────────────────────────────────
    {
        "key": "social_battery",
        "category": "Social",
        "question": "How do you recharge your energy?",
        "allow_multiple": False,
        "options": [
            {"id": "introvert", "label": "Alone time — I need solitude to recharge"},
            {"id": "extrovert", "label": "Being around people energizes me"},
            {"id": "ambivert", "label": "Depends on my mood and the people"},
            {"id": "nature", "label": "Being outdoors / in nature"},
            {"id": "creative", "label": "Doing something creative or productive"},
        ],
    },
    {
        "key": "trust_building",
        "category": "Social",
        "question": "What makes you trust someone?",
        "allow_multiple": True,
        "options": [
            {"id": "consistency", "label": "Consistency over time"},
            {"id": "honesty", "label": "They're honest even when it's hard"},
            {"id": "actions", "label": "Actions match their words"},
            {"id": "vulnerability", "label": "They're willing to be vulnerable"},
            {"id": "loyalty", "label": "They have my back when I'm not around"},
            {"id": "competence", "label": "They're competent at what they do"},
        ],
    },
    {
        "key": "helping_others",
        "category": "Social",
        "question": "When someone asks for help, you usually...",
        "allow_multiple": False,
        "options": [
            {"id": "always", "label": "Help immediately — I drop what I'm doing"},
            {"id": "consider", "label": "Help if I can, but check my capacity first"},
            {"id": "boundaries", "label": "Help within my boundaries — I don't overextend"},
            {"id": "teach", "label": "Prefer to teach them how to do it themselves"},
            {"id": "depends", "label": "Depends on who's asking and what it is"},
        ],
    },

    # ── Work & Productivity ────────────────────────────────────────
    {
        "key": "work_style",
        "category": "Work & Productivity",
        "question": "How do you prefer to work?",
        "allow_multiple": True,
        "options": [
            {"id": "deep_focus", "label": "Long uninterrupted deep focus blocks"},
            {"id": "pomodoro", "label": "Short sprints with breaks"},
            {"id": "flexible", "label": "Flexible — I work when inspiration strikes"},
            {"id": "structured", "label": "Structured schedule with clear routines"},
            {"id": "collaborative", "label": "Collaboratively — I bounce ideas off others"},
            {"id": "async", "label": "Independently & async — no meetings please"},
        ],
    },
    {
        "key": "motivation_source",
        "category": "Work & Productivity",
        "question": "What motivates you most?",
        "allow_multiple": True,
        "options": [
            {"id": "mastery", "label": "Getting really good at something"},
            {"id": "impact", "label": "Seeing my work make a difference"},
            {"id": "recognition", "label": "Being recognized for my contributions"},
            {"id": "money", "label": "Financial rewards"},
            {"id": "autonomy", "label": "Having freedom in how I work"},
            {"id": "team", "label": "Working with a great team"},
            {"id": "challenge", "label": "Tackling hard problems"},
        ],
    },
    {
        "key": "failure_response",
        "category": "Work & Productivity",
        "question": "When you fail at something important, what do you do first?",
        "allow_multiple": False,
        "options": [
            {"id": "analyze", "label": "Analyze exactly what went wrong"},
            {"id": "try_again", "label": "Try again immediately with a different approach"},
            {"id": "step_back", "label": "Step back and reassess if it's worth pursuing"},
            {"id": "talk", "label": "Talk it through with someone"},
            {"id": "rest", "label": "Give myself time to process before acting"},
        ],
    },

    # ── Lifestyle & Preferences ────────────────────────────────────
    {
        "key": "morning_or_night",
        "category": "Lifestyle",
        "question": "When are you at your best?",
        "allow_multiple": False,
        "options": [
            {"id": "early_bird", "label": "Early morning — I'm a dawn person"},
            {"id": "morning", "label": "Mid-morning — after coffee kicks in"},
            {"id": "afternoon", "label": "Afternoon — I peak mid-day"},
            {"id": "evening", "label": "Evening — I come alive at night"},
            {"id": "night_owl", "label": "Late night — the world is quiet and I focus"},
        ],
    },
    {
        "key": "stress_response",
        "category": "Lifestyle",
        "question": "When you're stressed, you tend to...",
        "allow_multiple": True,
        "options": [
            {"id": "exercise", "label": "Exercise or move my body"},
            {"id": "isolate", "label": "Withdraw and process alone"},
            {"id": "talk", "label": "Talk to someone about it"},
            {"id": "distract", "label": "Distract myself (shows, games, etc.)"},
            {"id": "plan", "label": "Make a plan to tackle the source"},
            {"id": "sleep", "label": "Sleep it off"},
            {"id": "create", "label": "Channel it into something creative"},
        ],
    },
    {
        "key": "learning_style",
        "category": "Lifestyle",
        "question": "How do you learn best?",
        "allow_multiple": True,
        "options": [
            {"id": "doing", "label": "By doing — hands-on trial and error"},
            {"id": "reading", "label": "By reading & researching"},
            {"id": "watching", "label": "By watching videos / tutorials"},
            {"id": "teaching", "label": "By teaching others what I know"},
            {"id": "discussing", "label": "By discussing with others"},
            {"id": "structured", "label": "Structured courses with clear progression"},
        ],
    },

    # ── Scenario-based ─────────────────────────────────────────────
    {
        "key": "scenario_friend_money",
        "category": "Scenarios",
        "question": "A close friend asks to borrow a significant amount of money. You...",
        "allow_multiple": False,
        "options": [
            {"id": "lend_full", "label": "Lend the full amount — they're my friend"},
            {"id": "lend_partial", "label": "Offer a smaller amount I'm comfortable losing"},
            {"id": "gift", "label": "Give it as a gift — I don't lend to friends"},
            {"id": "decline", "label": "Politely decline — money and friendships don't mix"},
            {"id": "help_other", "label": "Help them find another solution instead"},
        ],
    },
    {
        "key": "scenario_job_offer",
        "category": "Scenarios",
        "question": "You get a dream job offer but it means moving far from family. You...",
        "allow_multiple": False,
        "options": [
            {"id": "take_it", "label": "Take it — this is my chance, family understands"},
            {"id": "negotiate", "label": "Try to negotiate remote or hybrid first"},
            {"id": "decline", "label": "Decline — being near family is more important"},
            {"id": "temporary", "label": "Take it temporarily — try it for a year"},
            {"id": "discuss", "label": "Have a deep discussion with family before deciding"},
        ],
    },
    {
        "key": "scenario_wrong_order",
        "category": "Scenarios",
        "question": "A restaurant gets your order wrong. You...",
        "allow_multiple": False,
        "options": [
            {"id": "polite_fix", "label": "Politely ask them to fix it"},
            {"id": "eat_it", "label": "Eat it anyway — not worth the hassle"},
            {"id": "mention_later", "label": "Mention it casually but don't push"},
            {"id": "firm", "label": "Firmly send it back — I ordered what I ordered"},
            {"id": "depends", "label": "Depends on how different it is"},
        ],
    },
    {
        "key": "scenario_disagreement_boss",
        "category": "Scenarios",
        "question": "Your boss makes a decision you strongly disagree with. You...",
        "allow_multiple": False,
        "options": [
            {"id": "speak_up", "label": "Speak up directly in the meeting"},
            {"id": "private", "label": "Bring it up privately afterwards"},
            {"id": "comply", "label": "Go along with it — they're the boss"},
            {"id": "data", "label": "Prepare data supporting your alternative and present it"},
            {"id": "allies", "label": "Rally colleagues who agree to make a collective case"},
        ],
    },
    {
        "key": "scenario_unexpected_free_day",
        "category": "Scenarios",
        "question": "You unexpectedly get a full day free with no obligations. You...",
        "allow_multiple": False,
        "options": [
            {"id": "productive", "label": "Tackle things I've been putting off"},
            {"id": "relax", "label": "Full relax mode — couch, shows, nothing"},
            {"id": "adventure", "label": "Spontaneous adventure or outing"},
            {"id": "social", "label": "Call up friends or family to hang out"},
            {"id": "hobby", "label": "Deep-dive into a hobby or side project"},
            {"id": "mix", "label": "A bit of everything — productive morning, chill afternoon"},
        ],
    },
]


def get_or_create_profile(db: Session, user_id: int) -> CloneProfile:
    profile = db.query(CloneProfile).filter(CloneProfile.user_id == user_id).first()
    if not profile:
        profile = CloneProfile(user_id=user_id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def get_progress(db: Session, profile_id: int) -> dict:
    """Return overall progress and per-category breakdown."""
    answered_keys = set(
        row[0]
        for row in db.query(CloneQA.question_key)
        .filter(CloneQA.profile_id == profile_id)
        .all()
    )
    total = len(QUESTION_BANK)
    done = sum(1 for q in QUESTION_BANK if q["key"] in answered_keys)

    categories: dict[str, dict] = {}
    for q in QUESTION_BANK:
        cat = q["category"]
        if cat not in categories:
            categories[cat] = {"total": 0, "done": 0}
        categories[cat]["total"] += 1
        if q["key"] in answered_keys:
            categories[cat]["done"] += 1

    return {
        "total": total,
        "done": done,
        "percent": round(done / total * 100) if total else 0,
        "categories": categories,
        "answered_keys": list(answered_keys),
    }


def get_next_unanswered(db: Session, profile_id: int, after_key: Optional[str] = None) -> Optional[dict]:
    """Return the next unanswered question, optionally starting after a given key."""
    answered_keys = set(
        row[0]
        for row in db.query(CloneQA.question_key)
        .filter(CloneQA.profile_id == profile_id)
        .all()
    )

    started = after_key is None
    for q in QUESTION_BANK:
        if not started:
            if q["key"] == after_key:
                started = True
            continue
        if q["key"] not in answered_keys:
            return q

    if after_key is not None:
        for q in QUESTION_BANK:
            if q["key"] not in answered_keys:
                return q

    return None


def get_question_by_key(key: str) -> Optional[dict]:
    for q in QUESTION_BANK:
        if q["key"] == key:
            return q
    return None


def save_answer(
    db: Session,
    profile_id: int,
    question_key: str,
    selected_options: list[str],
    freeform_answer: str = "",
) -> CloneQA:
    """Save or update an answer for a question."""
    q = get_question_by_key(question_key)
    if not q:
        raise ValueError(f"Unknown question key: {question_key}")

    existing = (
        db.query(CloneQA)
        .filter(CloneQA.profile_id == profile_id, CloneQA.question_key == question_key)
        .first()
    )

    if existing:
        existing.selected_options = json.dumps(selected_options)
        existing.freeform_answer = freeform_answer.strip()
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    entry = CloneQA(
        profile_id=profile_id,
        question_key=question_key,
        category=q["category"],
        question_text=q["question"],
        selected_options=json.dumps(selected_options),
        freeform_answer=freeform_answer.strip(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_all_answers(db: Session, profile_id: int) -> list[dict]:
    """All answers for export / review."""
    rows = (
        db.query(CloneQA)
        .filter(CloneQA.profile_id == profile_id)
        .order_by(CloneQA.created_at)
        .all()
    )
    results = []
    for r in rows:
        q = get_question_by_key(r.question_key)
        selected = json.loads(r.selected_options) if r.selected_options else []
        selected_labels = []
        if q:
            opt_map = {o["id"]: o["label"] for o in q["options"]}
            selected_labels = [opt_map.get(s, s) for s in selected]
        results.append({
            "id": r.id,
            "question_key": r.question_key,
            "category": r.category,
            "question": r.question_text,
            "selected_options": selected,
            "selected_labels": selected_labels,
            "freeform": r.freeform_answer or "",
            "updated_at": r.updated_at.isoformat() if r.updated_at else "",
        })
    return results


def get_answer_for_key(db: Session, profile_id: int, question_key: str) -> Optional[dict]:
    """Get existing answer for a specific question."""
    row = (
        db.query(CloneQA)
        .filter(CloneQA.profile_id == profile_id, CloneQA.question_key == question_key)
        .first()
    )
    if not row:
        return None
    return {
        "selected_options": json.loads(row.selected_options) if row.selected_options else [],
        "freeform": row.freeform_answer or "",
    }
