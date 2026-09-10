"""Decompose-call prompt text — DESIGN.md §6.2 field semantics.

INSTRUCTIONS is the static block (keep it byte-stable between commits — prompt
caching matches exactly at breakpoints, §6.1). render_context() produces the
per-user context block that follows it.
"""

INSTRUCTIONS = """\
You decompose one food-log utterance (spoken or typed) into structured items.
Output must conform exactly to the provided schema. Extract only what was
actually said — never infer, estimate, or invent anything.

Per-field rules:

items — one entry per distinct food or saved-meal reference in the utterance.
Do not merge distinct foods into one item; do not add items that were not
spoken.

phrase — the food phrase with quantity words removed ("two eggs" -> "eggs";
"40 grams of whey" -> "whey"). Keep brand and descriptor words as spoken. For
a meal reference, the phrase is the meal phrase without articles/possessives
("my usual breakfast" -> "usual breakfast").

phrase_kind — "meal_ref" when the phrase refers to a named meal or saved
combination of foods rather than a single food ("my usual breakfast", "the
usual", "my post-workout meal"). Otherwise "food".

qty — a number ONLY when the user spoke an explicit quantity ("two eggs" ->
2; "40g" -> 40; "half a cup" -> 0.5). If no quantity was spoken, qty MUST be
null. Never invent, infer, or default a quantity. The article "a"/"an" is not
a quantity.

unit — the unit word as spoken: "bottle", "scoop", "g", "oz", "cup", etc.
Abbreviating a spoken unit is fine ("grams" -> "g"); inventing one is not. A
bare count has no unit: "two eggs" -> qty 2, unit null.

state — "cooked", "raw", or "dry" ONLY when the user said it or a synonym
(grilled/roasted/boiled/baked -> "cooked"; uncooked -> "raw"; dry weight ->
"dry"). Otherwise null. Never assume a state from the food alone.

brand — the brand name if one was spoken ("fairlife", "Quest"), else null.

library_match — the PERSONAL FOOD INDEX below lists foods the user logs
often, and RECENTLY LOGGED FOODS lists what they logged lately (newest
first); both use one line per food: index_id | display_name | brand. If the
item's phrase is semantically the same food as one entry (e.g. "protein
shake" when the index holds the shake they always log; "fairlife" when a
fairlife milk was logged recently), set library_match to that entry's
index_id, copied verbatim. Only ids that appear in one of the two lists are
allowed; if no entry is clearly the same food, use null.

meal_ref — the SAVED MEALS list below names the user's saved meals. If the
phrase refers to one of them, set meal_ref to that name exactly as listed;
otherwise null. A phrase_kind of "meal_ref" with no matching saved meal keeps
meal_ref null.

meal_hint — the meal ONLY if the user named it ("for breakfast I had...");
otherwise null. Never infer it from the food or time of day.

when_hint — the spoken time reference, verbatim ("this morning", "yesterday
lunch"); null if none was spoken.

when_day_offset — that same reference resolved to a WHOLE NUMBER OF DAYS back
from the local date on the NOW line at the end of this input: 0 = that day,
-1 = the day before it, -2 = two days before, and so on. Work it out for
whatever was said — "yesterday" is -1, "last night" is -1, "this morning" is
0, "the day before yesterday" is -2, "two days ago" is -2, a weekday name is
the most recent PAST day with that name. Null ONLY when no time reference was
spoken at all. Never a positive number: food is always already eaten. Set
this even when the meal word sits elsewhere in the sentence ("Yesterday, I
also had two yogurts for breakfast" -> when_day_offset -1, meal_hint
"breakfast"). This field is about when the food was EATEN, never about when it
was cooked or bought: "the rice from last night", "leftover chicken from
Sunday", "that pizza from Friday" describe where the food came from, and if the
eating itself is present or unstated the offset is null. Only a time reference
that places the MEAL gets an offset.

when_meal — the meal that the TIME words themselves imply, when they do:
"this morning"/"yesterday morning" -> breakfast, "last night" -> snack,
"yesterday at lunch" -> lunch. Null when the time words say nothing about
which meal ("yesterday", "two days ago") or when none were spoken. This is
separate from meal_hint, which is only for a meal the user named outright;
when both are set meal_hint wins.

save_intent — ONLY when the user asked to save or name a meal ("save this as
my usual breakfast" -> {"name": "usual breakfast"}); otherwise null.
"""


def render_now(now_local) -> str:
    """The NOW line (§7.2, BACKLOG 189) — what `when_day_offset` counts back from.

    This rides with the UTTERANCE, never in the context block, because the
    context block carries an explicit prompt-cache breakpoint: a clock in there
    would change on every request and miss the cache every time.
    """
    return (
        f"NOW: {now_local:%A %Y-%m-%d %H:%M} local time. "
        "Resolve when_day_offset against this date."
    )


def _food_lines(foods) -> str:
    return "\n".join(
        f"{index_id} | {display_name} | {brand or '-'}" for index_id, display_name, brand in foods
    )


def render_context(ctx) -> str:
    """Context block (§6.1): profile notes, targets, food index, the recently
    logged foods, saved meals."""
    targets = (
        "\n".join(f"{k}: {v}" for k, v in sorted(ctx.targets.items()))
        if ctx.targets
        else "(none)"
    )
    meals = "\n".join(ctx.meal_names) if ctx.meal_names else "(none)"
    return (
        f"PROFILE NOTES:\n{ctx.profile_notes or '(none)'}\n\n"
        f"TARGETS:\n{targets}\n\n"
        f"PERSONAL FOOD INDEX (index_id | display_name | brand):\n"
        f"{_food_lines(ctx.food_index) or '(empty)'}\n\n"
        f"RECENTLY LOGGED FOODS (index_id | display_name | brand; newest first):\n"
        f"{_food_lines(ctx.recent_foods) or '(empty)'}\n\n"
        f"SAVED MEALS:\n{meals}"
    )
