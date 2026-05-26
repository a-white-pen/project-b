# Attention taxonomy (v3)

The canonical reference for `b.attention_sessions.(category, subcategory)`. Mirrors the
DB CHECK constraint `attention_sessions_taxonomy_check` and the `_TAXONOMY` dict in
`domains/attention/service.py`. When proposing a schema change, edit this file in the
same PR.

**Rules:**
- One primary `(category, subcategory)` pair per session. No concurrent activities.
- Co-categorisations of the same activity (e.g. tennis with friends = exercise + social_in_person) go in `notes` as `+ main : sub` markers and render as `also: main : sub` in the bubble body.
- Subcategory must form a valid pair with its main — enforced by `attention_sessions_taxonomy_check`.
- Underscore in stored name; display converts main underscore to space (e.g. `self_care` → "self care"). Subcategory shown verbatim.

---

## Categories

| Color | Main category | Subcategories | Description examples |
|---|---|---|---|
| 🟦 | Work | `deep_work` | work on project b, deep work on nutrition module, work on job applications, working on attention module improvements |
|  |  | `shallow_work` | chatting with chatgpt, shallow work on project b, nutrition fixes and update finances |
|  |  | `meetings` | meeting about project b roadmap, catch up with manager, interview for data scientist role, standup |
|  |  | `learning` | watch YouTube video on MCP servers, read ISLP textbook chapter 3, DataCamp lesson on regression |
|  |  | `planning` | planning gym session, plan travel back to singapore, planning day, plan air ticket |
| 🟪 | Social | `social_in_person` | AI meetup, racket sports with friends, dinner with friends, lunch with friends, running with friends |
|  |  | `social_messaging` | reply messages, email friends, phone call with friends, video call / FaceTime / Zoom with friends, catch up call |
|  |  | `social_broadcast` | update weekend IG, update Strava, post to friends story, public-facing post on personal accounts |
| 🟩 | Self-care | `exercise` | gym run, weight training, running, gym exercise (weight day) |
|  |  | `personal_care` | shower, brush teeth, wash face, cut toe nails, prep to go out, massage, physiotherapy, poop, pee |
|  |  | `meditation` | meditate, breathing exercise |
| 🟧 | Eat | `food_prep` | prep breakfast, heat up dinner, wash dishes, make protein shake, wash blueberries |
|  |  | `food_collection` | collect lunch from downstairs, order food on Grab / Lineman / Robinhood |
|  |  | `eating` | eat breakfast, eating lunch, post-workout snack, eat dinner, drinking protein shake |
| 🟨 | Downtime | `rest` | nap, taking a break, resting |
|  |  | `entertainment` | watching tv, scrolling Instagram |
| 🟫 | Admin | `shopping_online` | Shopee / Lazada order, research standing desk, compare protein powder, searching for flights, online browsing |
|  |  | `shopping_in_store` | shopping at Tops, in-store grocery, go 7-11 buy things, walk-in browsing |
|  |  | `errands` | do laundry, throw rubbish, change toilet light bulb, collect parcel |
|  |  | `life_admin` | update finances, renew passport, track delivery |
|  |  | `health_admin` | doctor visit, dentist appointment, therapy session, pharmacy run |
| ⬜ | Transit | `commute` | take MRT, grab to nearby, bus to gym, car to office |
|  |  | `travel` | flight to Singapore, overnight train to Chiang Mai, long drive to Hua Hin |
| ⬛ | Other | `other` | anything that doesn't fit |

**Totals: 8 main × 24 sub = 24 valid pairs.**

---

## Disambiguation notes

- **Calls (video, voice, FaceTime, Zoom) with friends** → `social_messaging`. Treated as communicating with friends through a device. `social_in_person` is reserved for actual physical presence.
- **Replying to a friend's IG comment on your post** → `social_messaging` (you're messaging that person), not `social_broadcast`.
- **Reading IG feed / TikTok / TV** → `downtime / entertainment`. Not social — passive consumption.
- **Going to a concert / event with friends** → `social_in_person` if the focus is the friends + shared experience; `entertainment` if the focus is the event itself. Judgment call. Default: `social_in_person` if friends are central.
- **"pong pong"** (B's term for shower/bathe) → `self_care / personal_care`.
- **"mum mum"** (B's term for eating) → `eat / eating`.
- **Coffee break** → `downtime / rest` (unless you're separately logging the drink as nutrition).
- **Online research for a purchase you might make** → `shopping_online`. Switches to `learning` only if the research has no purchase intent (e.g., reading reviews of a category for general knowledge).
- **Going to the 7-11 to throw rubbish on the way** → still `errands` (rubbish is primary). Going specifically to 7-11 to buy something → `shopping_in_store`.
- **Cooking** → `food_prep`. **Picking up lunch downstairs / Grab order** → `food_collection`. **Sitting down and consuming the food** → `eating`.

---

## Co-categorisation examples

Stored as `+ main : sub` markers in `notes`, one per line. Multiple allowed. Self-collision (adding the primary as a co-cat) silently dropped.

| Activity | Primary | Co-categorisations |
|---|---|---|
| Playing tennis with friends | `self_care : exercise` | `+ social : social_in_person` |
| Cooking dinner while on the phone with mum | `eat : food_prep` | `+ social : social_messaging` |
| Walking to gym while listening to a podcast | `transit : commute` | `+ work : learning` (if educational) or `+ downtime : entertainment` |

---

## Migration history

- **v3** (this version): added `eat / food_collection`; split `social / social` into `social_in_person / social_messaging / social_broadcast`; split `admin / shopping` into `shopping_online / shopping_in_store`; reclassified "7-11" examples from errands to shopping_in_store; added "poop" to personal_care.
- **v2**: split flat `category` into `(category, subcategory)`. 21 valid pairs. See git history of `schema/data_dictionary.md` for the migration SQL.
- **v1**: flat single-column `category` with 15 values (deep_work, shallow_work, planning, learning, exercise, cooking, eating, commute, life_admin, personal_care, social, entertainment, rest, meditation, other).

---

## Source of truth precedence

If any two of these disagree, this is the resolution order:
1. **`attention_sessions_taxonomy_check`** — the DB constraint. Whatever Postgres accepts is canonical.
2. **`_TAXONOMY` dict** in `domains/attention/service.py` — must mirror the DB.
3. **This file** — narrative reference. Update whenever (1) and (2) change.
4. **The LLM extraction prompt** in `service.py` and `correction.py` — must list the same taxonomy with examples drawn from this file.

When changing the taxonomy, the order of operations is:
1. Edit this file with the proposed change
2. Propose schema SQL (DROP CHECK + UPDATE backfill + ADD CHECK + COMMENT ON COLUMN)
3. Wait for B to apply + regenerate `schema/data_dictionary.md`
4. Update `_TAXONOMY`, `_CATEGORY_EMOJI` (if main changes), and both LLM prompts in code
5. Update / add tests
