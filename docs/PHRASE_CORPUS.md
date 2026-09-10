# Phrase corpus — 200 utterances in Bill's voice

## The short version

These are 200 things Bill would actually say or type at Tally, written the way they
arrive at the API: no punctuation, filler kept in, brands mangled by Deepgram, numbers
turned into the wrong words. They exist because of [issue #21](https://github.com/IdkwhatImD0ing/nutritiontracker/issues/21):
one ordinary sentence hit four defects at once, and every one of those defects passed CI,
because the tests were built from tidy fixtures instead of from how a person talks.

Twelve lanes, split so no single kind of phrase dominates. Of the 200, 88 are messy-but-
correctly-transcribed speech, 88 are mis-transcribed by the speech-to-text engine, and 24
are typed with keyboard typos. Each carries a one-line `hazard`: what that specific phrase
can break on the way to a diary row with calories, protein, carbs and fat.

The machine-readable copy is [backend/tests/fixtures/phrase_corpus.json](../backend/tests/fixtures/phrase_corpus.json),
so a table test can drive the pipeline straight off it. Findings from tracing these through
the code are in [PHRASE_FINDINGS.md](PHRASE_FINDINGS.md).

| Lane | Phrases | What it stresses |
|---|---|---|
| Morning — the first log of the day | 17 | half-awake logs, no amounts, coffee identity |
| Shakes, powder, and a weigh-in in the same breath | 16 | scoops, halves, and a body weight in the same utterance |
| Drive-thru and coffee chains | 17 | menu items, sizes as words, custom orders, shared orders |
| Packaged groceries — Costco, Trader Joe's, the fridge | 17 | package fractions, label serving vs container, mangled brands |
| Backdated — a day word and a meal word, in either order | 17 | day word and meal word separated, 1am logs about yesterday |
| The usual — saved meals and repeats | 17 | tier-0 saved meals, subtractive edits, undefined copy scope |
| Amounts — counts, containers, fractions, made-up units | 17 | bare counts, containers, ranges, units on no label |
| Corrections and follow-up turns | 17 | second-turn corrections, deletes, which row do I mean |
| Unlabeled food — homemade, restaurant, leftovers | 17 | no label anywhere, the estimate lane |
| Pure speech-to-text wreckage | 16 | homophone numbers, split and merged words, dictated punctuation |
| Not a food log at all | 16 | questions, weight-only, app commands, empty transcripts |
| Nasty edges a real person still says | 16 | negation, sharing, zero-macro foods, one giant run-on |

---

## Morning — the first log of the day

**P001** (speech)  
> shake

Bare noun with no brand, size, or scoop count — nothing to anchor macros to unless it resolves to a saved meal.

**P002** (speech)  
> um two eggs and uh some rice from last night i think thats it for now

"some rice from last night" has no weight and its "last night" may be read as a diary-date shift on a breakfast log.

**P003** (speech)  
> just coffee so far like the big one from starbucks the usual

Drink identity unresolved — Venti size plus milk and syrup choices decide every macro, and "the usual" needs history lookup.

**P004** (speech)  
> okay so the yogurt thing and a banana and uh also i weighed like one seventy eight this morning

Body weight is crammed into a food log with no unit; 178 must become a weight entry, not a food row with 178 grams.

**P005** (speech)  
> three eggs no wait four eggs scrambled with a handful of cheddar and uh some hot sauce doesnt count right

Spoken self-correction must replace the count, not sum to seven eggs; "a handful of cheddar" has no gram estimate.

**P006** (speech)  
> oatmeal with peanut butter in it like a big scoop and half a scoop of the powder the vanilla one

Two different "scoop" units in one breath — a PB spoonful versus a fractional protein-powder scoop of an unnamed brand.

**P007** (speech)  
> same as always

Pure back-reference with zero items named; requires resolving his usual breakfast from history or the log is empty.

**P008** (speech)  
> premier protein on the way out the door thats it im gonna eat real food when i get there

Premier Protein size and flavor unresolved (11oz vs 14oz), and the "gonna eat later" clause is future intent that must not be logged.

**P009** (speech)  
> i think i ate a bowl of cereal at like four am does that go on today or yesterday cause i hadnt slept yet

4am log straddles the calendar boundary — his subjective day versus the America/Los_Angeles date, and the question needs an answer not a row.

**P010** (stt)  
> to eggs and won fair life shake the chocolate one i had it in bed

"to" and "won" are number-word homophones for 2 and 1; "fair life" must alias to Fairlife or the brand lookup fails.

**P011** (stt)  
> i coast triple zero and ate almonds maybe more than ate whatever

"i coast" is Oikos mangled and "ate" is the number 8 twice; "maybe more than ate" leaves the almond count non-deterministic.

**P012** (stt)  
> a big bowl of oh cost with granola on top and a la kwa after

Two mangled brands (Oikos, La Croix) plus "big bowl" with no gram weight, and granola is a second unpriced item hiding in a modifier.

**P013** (stt)  
> guest bar in the car and coffee no sugar kirk land the big bag one

"guest" is Quest with no flavor picked, and "kirk land the big bag one" describes coffee beans rather than a servable portion.

**P014** (stt)  
> chick filet chicken minis this morning the ate count and a large coffee

"the ate count" is an 8-piece order transcribed as a verb; needs a restaurant-item match, not generic chicken plus bread.

**P015** (stt)  
> wait was one seventy seven this morning and then i had to scrambled eggs and half a bagel

weight/wait homophone can drop the body-weight reading entirely, while "to scrambled eggs" is really a count of two.

**P016** (stt)  
> fah gay yogurt with honey like a spoonful and a sigh bowl thing my girlfriend made

"fah gay" is Fage and "a sigh" is acai; a girlfriend-made bowl means an unknown recipe and an unknown split portion.

**P017** (stt)  
> um i just had like 8 protein shake and uh whatever was left of the rice from last night

The article "a" was heard as the digit 8, so quantity reads as eight shakes; "whatever was left" has no measurable amount.

## Shakes, powder, and a weigh-in in the same breath

**P018** (speech)  
> the usual shake and creatine

"usual" needs a Tier-0 saved-meal hit; creatine has no macros so it becomes a 0-kcal row or is silently dropped.

**P019** (speech)  
> um my shake but i only drank like half of it cause it tasted weird so half a shake i guess

Portion is 0.5 of an unknown baseline; the restated "half a shake" risks halving twice to 0.25.

**P020** (speech)  
> protein shake last night around midnight i forgot to log it two scoops chocolate with fairlife and a spoon of peanut butter and a banana in the blender and also im 183 this morning

One breath yields four food rows plus a weight entry, and "last night around midnight" straddles two diary dates.

**P021** (speech)  
> one scoop no wait two scoops i did two

Spoken self-correction — naive extraction sums 1+2 and logs three scoops.

**P022** (speech)  
> me and her split a premier protein so half a bottle

Halving a fixed-size bottle whose label is per-container, so 0.5 must scale the package panel, not a serving.

**P023** (speech)  
> shake and im 182 today

182 must route to body weight in pounds, not become the shake's grams or calories.

**P024** (speech)  
> pre workout only the powder no calories right just log it anyway

User demands a row with all-zero macros; a 0-kcal item may fail validation or disappear from the diary total.

**P025** (speech)  
> big scoop like a heaping one not the flat scoop it says on the tub

"Heaping" is a multiplier on the tub's serving grams with no number and no named product to look up.

**P026** (stt)  
> to scoops of way protein with fair life whole milk and ate ounces of water

"to"=2, "ate"=8, "way"=whey, "fair life"=fairlife, and milk plus water both claim the liquid base.

**P027** (stt)  
> i had my guest powder shake and my wait was won eighty won this morning

"guest"=Quest, "wait"=weight, "won eighty won"=181 — the number can be misread as a food quantity.

**P028** (stt)  
> kirk land way vanilla to scoops in the shaker and a little a sigh packet thing

"kirk land" and "a sigh" are Kirkland and acai; the acai packet has no size, so grams are unknowable.

**P029** (stt)  
> creating five grams and pre workout the blue one before the gym

"creating"=creatine at 0 kcal; "the blue one" gives no brand to match a pre-workout row.

**P030** (stt)  
> premier protein caramel won bottle and i ate half of another won

"won" is one twice while "ate" is the verb, not eight — the real quantity is 1.5 bottles.

**P031** (stt)  
> shake with fah gay yogurt in it and a scoop of chocolate way

"fah gay"=Fage; the yogurt amount is missing and it is blended in, so one composite row or two separate ones is undecided.

**P032** (stt)  
> casey in shake before bed to scoops in water

"casey in"=casein, different macros than whey; "to"=2 and a bedtime log can land on the wrong diary date.

**P033** (stt)  
> 8 scoop and a half of the chocolate one and some milk whatever was left in the jug

"8" is a misheard "a", so 1.5 scoops not 8.5; "whatever was left" gives no milk volume.

## Drive-thru and coffee chains

**P034** (speech)  
> uh chick fil a nuggets the twelve piece and a large fry and the sauce like two of the chick fil a sauces

12-count could be grilled or breaded (about 100 kcal apart) and two sauce packets are their own ~280 kcal row that gets dropped as a condiment

**P035** (stt)  
> chick filet grilled chicken sammich no pickles and a lemonade i think it was the small won

'won' parses as quantity 1 instead of drink size small; 'no pickles' is a zero-macro modifier that should not force a different food match

**P036** (typed)  
> went thru the cfa drive thru aroubd 11 last nite and got the 30 count nuggets for me n her so i had like half of that plus 2 sauces

'cfa' abbreviation plus 'last nite' at an after-midnight log means the diary date shifts back a day, and 30-count split in half is 15 nuggets not 30

**P037** (speech)  
> ok im reading off the receipt uh eight count grilled nuggets a market salad large diet lemonade and a cool wrap but the cool wrap was hers so dont count that one

four line items read in a row with an exclusion instruction attached only to the last one; market salad also has a dressing that is unnamed

**P038** (speech)  
> venti iced brown sugar shaken espresso with oat milk and i had one of those bacon gouda egg bites no like two of them

self-correction changes egg bite count 1 to 2 but egg bites ship as a 2-pack, so 'two' may mean one package; oat milk is already the default for that drink

**P039** (stt)  
> grand day caramel macchiato with skim and ate pumps of vanilla no wait for pumps

'grand day' is grande, 'ate' and 'for' are 8 and 4 pumps at ~20 kcal each, and the corrected number comes after the wrong one

**P040** (typed)  
> sbux grande cold brew w sf vanilla and a splash of heavy cream mabye 2 splashes

'sf' means sugar free so the syrup is zero-cal, but 'splash' of heavy cream has no unit and 2 splashes could be 30 or 100 kcal

**P041** (speech)  
> oh yeah i forgot yesterday like two in the morning we hit in n out i got a double double protein style and animal fries but she ate half the fries

2am yesterday is two diary dates back from a 1am log; protein style drops the bun; animal fries are shared so the portion is 0.5

**P042** (stt)  
> in and out cheese burger no onion and mustard grilled and a neopolitan shake wait it was chocolate

'mustard grilled' is a cooking modifier not an added condiment, and the shake flavor is corrected mid-utterance so the first flavor must not become a second row

**P043** (speech)  
> double double

two words, no chain named, no size or drink; could match a saved meal or a bare burger with no fries context

**P044** (stt)  
> chip hotel burrito bowl steak brown rice black beans queso blanco on top and chips on the side but she got most of the chips

'chip hotel' is Chipotle mangled; queso and chips are separately priced add-ons, and 'most of the chips' leaves an undefined fraction under 0.5 for me

**P045** (speech)  
> weighed in at like one eighty two this morning and then for lunch i had a chipotle chicken burrito the whole thing with everything on it

body weight in pounds and a food share one utterance; 'everything on it' is not an ingredient list so rice, beans, salsa, cheese, sour cream all have to be assumed

**P046** (speech)  
> the usual chipotle one with the double steak

saved-meal lookup plus a modifier that changes the saved meal's protein; unclear whether to log the tier-0 row as-is or a variant

**P047** (speech)  
> mcdonalds like a mcchicken and a ten piece nugget and a medium fry and a diet coke thats it

'ten piece' could be the 10-piece nuggets alone or the 10-piece meal that already includes the fry and drink, which would double-count them

**P048** (stt)  
> mick donalds sausage egg mcmuffin to of them and a hash brown and a large coffee black

'to' is quantity 2 sitting after the food name; sausage egg mcmuffin vs plain sausage mcmuffin differ by an egg

**P049** (typed)  
> mcdicks large fry and a mcflurry oreo i only ate like half the flurry befoer it melted

slang brand 'mcdicks' will not string-match McDonald's, and the 0.5 multiplier applies to only one of the two items

**P050** (typed)  
> panda express plate orange chickenn and beijing beef w chow mein and a springroll

a plate is 2 entrees plus 1 side, so the entree portions are half-size each, and the spring roll count is unstated

## Packaged groceries — Costco, Trader Joe's, the fridge

**P051** (speech)  
> um two of those little oikos cups the triple zero ones and uh half a premier protein i couldnt finish it

half of an 11oz shake bottle needs a 0.5 multiplier on a container-unit food, and the yogurt flavor is never named

**P052** (speech)  
> whole tub of the fage plain the big one no wait not the whole thing like two thirds

spoken self-correction must overwrite the first quantity, and big one is a 32oz vs 17.6oz tub

**P053** (speech)  
> a bag of the trader joes cauliflower gnocchi the whole bag which is like i dunno two servings whatever put down the whole bag

label is per-serving so the bag needs a serving-count multiply, and the user asserts a count that may not match the package

**P054** (speech)  
> instant ramen but i only used like half the seasoning packet so probably less sodium idk

partial condiment changes sodium and fat but not calories, and there is no sub-component field on the ramen row

**P055** (speech)  
> costco eggs i had three and then like a coupla those chicken bakes no scratch the chicken bakes that was yesterday

the retraction moves only the second item to a different diary date while the eggs stay on today

**P056** (speech)  
> i split a bag of the trader joes mandarin orange chicken with her so half a bag for me and the rice was separate ill do that later

half-bag split with another person plus an explicit deferral of a second food that must not be logged

**P057** (speech)  
> quest bar the cookies and cream one and also like four of those kirkland mixed nut packets the little ones

little ones means 1oz snack packs, not the bulk jar serving, so count four against the wrong base unit

**P058** (speech)  
> the usual costco yogurt thing

usual alias with no brand and two candidates in history, Kirkland tub vs Oikos cups, with different macros

**P059** (speech)  
> like a third of the bag of trader joes dark chocolate peanut butter cups i was coding

fraction of a countable candy bag with unknown gram weight, so no piece count to anchor calories

**P060** (stt)  
> won hole fair life to percent chocolate and 8 scoop of that kirk land shake mix

won hole is one whole, to percent is 2% milkfat which changes fat, and 8 scoop is really a scoop

**P061** (stt)  
> ate a guest bar and to of the oh cost cups

ate reads as eight or as the verb, to is two, and guest and oh cost are Quest and Oikos

**P062** (stt)  
> half the tub of the fah gay total zero and a la kwa

Fage Total 0% mangled beyond brand match, and La Croix is a genuine all-zero row that a lookup miss will drop

**P063** (stt)  
> i coast triple zero strawberry to cups no wait won cup and the other won was hers

count drops from two to one because the second cup belongs to the girlfriend, stated after the number

**P064** (stt)  
> a sigh bowl from the costco freezer the hole packet plus i put kirk land granola on there like a quarter cup i gues

one utterance is two rows, an acai packet and an eyeballed quarter cup of granola with no weight

**P065** (stt)  
> the frozen street tacos from costco i had like ate of them there small

ate is eight or the verb, and there small is a size qualifier that changes per-taco grams

**P066** (stt)  
> coupla those trader joes chicken burger patties the chili lime won and 8 whole wheat bun

coupla is an imprecise 2, won is the flavor one, and 8 bun is a single bun that becomes a count of eight

**P067** (stt)  
> won of them big fair life core power the 42 gram won after i lifted

the 42g SKU must be told apart from the 26g bottle, and 42 gram could be parsed as a portion weight

## Backdated — a day word and a meal word, in either order

**P068** (speech)  
> um yesterday i had like chicken and rice for lunch and a shake after i lifted i forgot to log any of it

One day word covers two items, and "after i lifted" is a time cue with no clock, so the shake may collapse into the lunch row.

**P069** (speech)  
> ok its like one am so this is for yesterday not today two eggs and toast and uh a premier protein

"yesterday" at 1am America/Los_Angeles is wall-clock minus one day; a UTC day boundary shifts the diary date two days back.

**P070** (stt)  
> last night ate to bowls of ramen at like 3am i think it was won of those spicy ones

"ate to" is the quantity two and "won" is one, while a 3am "last night" already sits on today's calendar date.

**P071** (typed)  
> sundya breakfsat 3 eggs and a fairlife shake

Misspelled weekday token won't hit a day regex, and past Sunday vs upcoming Sunday is undecidable.

**P072** (speech)  
> the day before yesterday i went to chick filet got the spicy deluxe and a large fry i dont remember what else

Three-word relative date needs arithmetic, and "dont remember what else" invites the resolver to invent a phantom item.

**P073** (stt)  
> on friday night me and my girlfriend split a chip hotel burrito bowl and i had like half the chips

"friday night" could be one day back or eight, and "split" means half macros that get logged whole.

**P074** (speech)  
> saturday nothing much just a guest bar and a coffee whenever that was

Weekday with no meal word at all, and "whenever that was" removes any basis for picking a meal slot.

**P075** (stt)  
> two days ago whatever i had that morning like the i coast yogurt with granola on it

"two days ago" and "that morning" double-anchor the timestamp, and the granola has no portion.

**P076** (typed)  
> yestreday dinner was inn out double double no fries adn a diet coke

Typo'd day word plus a negation, so "no fries" can be parsed as a fries row.

**P077** (stt)  
> earlier i had a la kwa and uh nothing else really oh and a banana

"earlier" is unbounded and may mean before midnight; "nothing else" is then contradicted by the banana in the same breath.

**P078** (speech)  
> over the weekend i mean saturday and sunday i had the same thing both days panda express two entree plate

One utterance has to fan out into duplicate rows on two different diary dates.

**P079** (stt)  
> this morning i wait 187 and had my shake before the gym

wait/weight homophone sits right next to 187, so the body weight can be read as a food quantity.

**P080** (speech)  
> put this on thursday no wait wednesday lunch the leftover chicken and rice like a big bowl

Spoken day self-correction; if both weekdays get extracted the row lands on Thursday instead of Wednesday.

**P081** (typed)  
> log teh oikos to lastnight i forgot

"lastnight" is one unsplit token, there is no meal or quantity, and "to" reads like a destination field.

**P082** (stt)  
> a while ago i had a sigh bowl from that place i dunno maybe like for in the afternoon on tuesday

"a while ago" fights with "on tuesday", and "for" is four o'clock rather than a count.

**P083** (speech)  
> at like 3am i ate a coupla eggs and the rest of the rice so gonna say two servings

3am belongs to the previous eating day even though the calendar date already rolled over.

**P084** (typed)  
> monday kirkland trail mix like a handfull

Bare weekday with no meal slot and no gram weight, so both the date bucket and the portion are guesses.

## The usual — saved meals and repeats

**P085** (speech)  
> the usual

Bare alias with no meal slot and no saved-meal name; at 1am it also has to guess today vs yesterday for the diary date.

**P086** (speech)  
> uh yeah the usual breakfast but i skipped the rice part i think

Subtractive edit on a saved meal: rice component must be dropped and macros recomputed, and "i think" is hedged confidence on the removal.

**P087** (speech)  
> same as yesterday

Copy scope undefined - all of yesterday's rows, or only the meal slot matching right now?

**P088** (speech)  
> lemme just do my post workout one, prolly like an hour ago

Fuzzy alias plus a relative time that should set logged_at, not shift the diary date.

**P089** (speech)  
> the shake thing again

"again" is either a second serving stacked onto today's existing row or a fresh repeat of an older day.

**P090** (speech)  
> save this as my usual lunch

save_meal with no items named - must bind to the prior turn's staged rows or it writes an empty saved meal.

**P091** (speech)  
> ok so i had my usual dinner the chicken and rice thing but she ate like a third of it so whatever thats on her and then i also had that yogurt thing after so add that too and uh i was 188 this morning

Three intents in one breath: a saved meal scaled to two thirds, a second aliased item, and a 188 lb body-weight entry.

**P092** (speech)  
> whatever i logged last thursday for lunch just do that again

Needs a diary query by date and meal slot, then re-logging rows whose underlying foods may have been edited since.

**P093** (speech)  
> the usual but like a big bowl of it not the normal amount

Portion override on a saved meal with a fixed serving size; "big bowl" gives no unit or gram weight to scale by.

**P094** (stt)  
> same thing i had on munday for dinner i dont remember what it was

"munday" needs a weekday-to-date lookup (this Monday or last) against the diary, not a saved-meal name match.

**P095** (stt)  
> my usual chick filet order the spicy won

Saved restaurant meal vs a live Chick-fil-A menu lookup, and "the spicy won" picks which sandwich inside it.

**P096** (stt)  
> to scoops of the fair life shake thing like usual

"to scoops" is 2 or a mis-heard two/too; "fair life" may resolve to generic milk instead of the saved fairlife shake.

**P097** (stt)  
> the usual but ate it at like won am so put it on yesterday

Explicit backdate to yesterday - the staged rewrite must re-append the log keys or the diary-date shift is silently lost.

**P098** (stt)  
> usual brekfast wait no not brekfast the post work out one

Spoken retraction: the first saved meal must be discarded, not logged alongside the second.

**P099** (stt)  
> i coast and eggs same as always

"i coast" has to map to Oikos; Tier-0 saved rows never learn aliases, so this mishearing can never be remembered.

**P100** (stt)  
> do my usual but from chip hotel not the one i make

"chip hotel" is Chipotle; the same alias points at both a homemade saved meal and a restaurant bowl with different macros.

**P101** (stt)  
> my usual shake but with ate ounces of milk not twelve

Ingredient swap inside a saved meal - "ate ounces" is 8 oz, and changing milk volume moves all four macro totals.

## Amounts — counts, containers, fractions, made-up units

**P102** (speech)  
> uh two of the yogurt cups and like a handful of almonds i dunno maybe more than a handful

"two cups" is a container count not a mass, and "handful" has no gram value so almond fat/cal is a free variable

**P103** (stt)  
> ate ounces of chicken breast i think and to scoops of rice

"ate"=8 oz and "to"=2 both parse as words, and a rice "scoop" is not a label serving

**P104** (typed)  
> abowl of instant ramen and half the seasoing packet only

halving only the packet changes sodium and some cal but not the noodle block, so the row can't scale as one multiplier

**P105** (speech)  
> a couple three eggs whatever it was like three

"a couple three" collides 2 and 3 in one phrase with no resolution rule

**P106** (stt)  
> won hundred and fifty grahams of ground beef

"grahams" must map to grams not graham crackers, and "won hundred" is a spelled number

**P107** (typed)  
> 2 servigns of the kirkland trail mix which is probly more like 3

"servings" needs the product's own serving size to become grams, and he immediately overrides 2 with 3

**P108** (speech)  
> like a slice and a half of the pizza from last night maybe two slices

a pizza slice has no fixed mass and 1.5 vs 2 slices is a 33% cal swing left unresolved

**P109** (stt)  
> 300 to 400 g of rice depending on how hungry i was

an explicit range needs a midpoint or a prompt, and cooked vs dry rice at 350 g differs about threefold in calories

**P110** (speech)  
> scoop and a half

a unit with no food attached, and "scoop" only means anything after resolving which powder he means

**P111** (typed)  
> half a bag of the frozen dumplingss no idea how many that is like 8 or 10

half a bag and 8-10 pieces are two different quantity systems for the same item and may not agree

**P112** (stt)  
> to fair life bottles the big ones not the little ones

"to"=2 plus a size qualifier that maps to 52 oz vs 11.5 oz, a 4x protein difference

**P113** (speech)  
> a plate of chicken and rice you know like a normal plate not a huge one

one container amount has to be split across two foods with no ratio given

**P114** (stt)  
> 8 sigh bowl from the place my girlfriend and i split it so half

"8 sigh"=acai with a stray 8 that reads as quantity, and the real amount is half of one bowl

**P115** (typed)  
> threequarters of a chipotle bowl the rest went in the fridge

0.75 of a multi-ingredient bowl scales every component, and the bowl's own contents are unknown

**P116** (speech)  
> one packet of oatmeal or was it two packets i think two

spoken self-correction 1 then 2, and "packet" only works if the packet weight is known

**P117** (stt)  
> a few slices of the sourdough like for or five

"for"=four, "a few" conflicts with 4-5, and slice thickness sets the carb count

**P118** (typed)  
> like 1.5 chicken thighs cus i ate part of hers too

a fractional count of a variable-weight item with bone-in vs boneless unknown

## Corrections and follow-up turns

**P119** (speech)  
> no wait not two scoops one scoop i already had a shake earlier so just one

Portion edit names no food; resolver must pick which prior row and decide if "shake earlier" is a separate row to keep.

**P120** (speech)  
> scratch that whole breakfast thing lemme start over

Bulk delete scoped to a meal slot, but at a 1am logging time the breakfast slot boundary and diary date are undefined.

**P121** (speech)  
> actually i only ate like half that burrito so uh cut it in half i guess

Halving is ambiguous between servings and grams, and "that burrito" collides if a bowl and a burrito are both logged.

**P122** (speech)  
> hold on i split that with my girlfriend so only half of it counts for me

Pronoun "that" has no antecedent in this turn; halving a saved meal must scale its components, not delete the row.

**P123** (speech)  
> um the ramen was two packs not one i always do two you know

Unit is "packs" not grams, and doubling has to include the seasoning packet sodium, not just noodle macros.

**P124** (speech)  
> wait no the weight thing was one eighty seven not one seventy eight i flipped it

Targets a body-weight entry, not a food row; the transposed digits mean both values are plausible so it can't self-validate.

**P125** (speech)  
> delete everything after the eggs that was me talking to my girlfriend not logging

Positional delete needs stable row ordering from one utterance, and the cutoff row itself must survive.

**P126** (speech)  
> no no the milk is the fairlife not regular whole milk i never buy regular

Brand swap with no quantity restated; portion must carry over while the food identity and macros are re-resolved.

**P127** (speech)  
> the yogurt one change it to two i had the other one at like midnight whatever

Two yogurt rows make the target ambiguous, and "at like midnight" may mean a second row on the previous diary date instead of a count bump.

**P128** (stt)  
> that was yesterday not today the chicken and rice thing was last night

Whole-utterance date shift; moving the row must preserve log keys or the diary-date change is silently dropped.

**P129** (stt)  
> i said fair life not far life the protein is way higher on the fair life

"far life" is not in the alias table, so the fix has to rewrite an already-resolved wrong brand and its stored macros.

**P130** (stt)  
> make it ate ounces not to ounces

Homophone digits: 8 vs 2 ounces is a fourfold portion error with no food named to sanity-check against.

**P131** (stt)  
> i said chicken not kitchen

Corrects the food identity, but "kitchen" likely resolved to nothing so there may be no row to patch.

**P132** (stt)  
> change the oh cost to won not three my bad i only ate won

Both the brand alias (Oikos) and the count (1 vs 3) are corrupted, so quantity and item resolution fail together.

**P133** (stt)  
> not chip hotel it was panda express i mixed them up delete the chip hotel one

One turn asks for a delete plus a replacement at a different restaurant with no menu item named.

**P134** (stt)  
> put the chick filet back i did eat it i deleted it by accident

Requires undoing a delete, so rows need soft-delete history and the original macros must be restorable, not re-resolved.

**P135** (stt)  
> the guest bar from tuesday delete it i never opened it

Delete targeted by weekday plus a mangled brand; two Quest bars on Tuesday leave the target ambiguous.

## Unlabeled food — homemade, restaurant, leftovers

**P136** (speech)  
> um so my mom made like a beef stew thing last night with potatoes in it and i had probably two bowls maybe three i dont know it was a lot

"two bowls maybe three" is a range with no bowl volume, and a homemade stew has no per-serving macro basis to scale.

**P137** (speech)  
> leftover chicken and rice from whatever my gf made

No ingredient list, no cooking method, no portion, and no date anchor for the leftovers.

**P138** (stt)  
> had a big plate of that curry at the place on sepulveda i dont even know whats in it like coconut milky and won piece of naan or maybe to

Restaurant identified by street, not menu; "won ... or maybe to" is 1-vs-2 naan as word-numbers inside one item.

**P139** (typed)  
> gf made pasta thing w ground beef and creaam sauce i ate like a plate and a hlaf

"plate and a hlaf" is a fractional non-unit; cream-sauce fat can vary 3x with no recipe to pin it.

**P140** (speech)  
> ok so uh yesterday i had my dads adobo over rice like a big bowl of it and then also a coupla lumpia and i weighed one seventy eight this morning

Body weight 178 lb hidden mid-food-list, and "yesterday" must retro-date the food rows but not the weigh-in.

**P141** (stt)  
> went to my aunts for dinner and there was some kind of braised pork with like ate different sides and i just ate whatever whole plate

"ate" is eight; eight unnamed side dishes become one unspecifiable diary row.

**P142** (speech)  
> smoothie i made with banana peanut butter spinach oats fair life and like a scoop of whatever protein was open, blender was full

Six ingredients, one quantity; "whatever protein was open" has no SKU and "blender was full" is the only serving size.

**P143** (typed)  
> potluck at works had somoene's enchilada casserole thing 2 squares maybe 3

"squares" depends on an unknown pan size and cut count; recipe is a stranger's.

**P144** (stt)  
> she made a stir fry with tofu and to kinds of veggies and i think sesame oil idk how much oil like a lot

"to kinds" is two unnamed vegetables, and unquantified cooking oil dominates the calorie estimate.

**P145** (speech)  
> two tacos from that little place by the laundromat no wait three tacos carne asada with the onions and cilantro

Spoken self-correction must replace the quantity 2 with 3, not sum to five tacos.

**P146** (speech)  
> mystery curry

Two words with no protein, no base, no portion: nothing to estimate from and no prior row to alias to.

**P147** (typed)  
> leftovres from the birthday dinner some kind of lamb and rice pilaff no idea portions i just ate til i was full

"til i was full" supplies zero quantity signal; both components are unlabeled restaurant food.

**P148** (stt)  
> the deli platter at the party had like turkey and hole wheat rolls and some pasta salad thing and i ate maybe won plate maybe too plates

"hole wheat" homophone, "won/too plates" number-words, and a platter is 3+ foods collapsed into one utterance.

**P149** (speech)  
> so uh this is for last night i forgot, my gf made a chicken thing baked with cheese on top and i had half the pan basically and some green beans

"half the pan" needs a pan size the model cannot know, and "for last night" shifts the diary date at 1am.

**P150** (stt)  
> made a a sigh bowl at home with frozen a sigh packets and granola and honey and a banana like a big bowl

"a sigh" is acai twice; the packets are labeled but the assembled bowl is not, so lanes conflict.

**P151** (typed)  
> moms oxtail soup like 2 bowls plus the rice she puts in it dont know how much rice

Rice is a nested sub-item inside the soup portion with an explicitly unknown amount.

**P152** (speech)  
> same soup as always the one my mom makes with the noodles, big bowl, and i think there was pork in it

"same as always" implies an alias lookup, but a homemade dish has no saved-meal row to resolve to.

## Pure speech-to-text wreckage

**P153** (stt)  
> won of those oh cost triple zero yogurts and to hard boiled eggs and a fair life

"won"/"to" are digits, and "a fair life" gives no size, so quantities and brand both need repair before lookup

**P154** (stt)  
> ate ounces of chicken and like a cup of rice period

"ate" reads as a verb but means 8 oz; trailing "period" is dictated punctuation that will land in the food name

**P155** (stt)  
> i had guest bar the chocolate chip won and a la kwa

"guest bar" is Quest bar and "la kwa" is La Croix; both mangled brands miss the food DB, and a sparkling water is a zero-cal row

**P156** (stt)  
> chick filet sandwich and 8 medium fry and a diet lemonade

"8" is the article "a" misheard as a number, so fries could get logged as eight servings

**P157** (stt)  
> so uh i think i ate the hole thing the whole bag of the kirk land trail mix

hole/whole homophone plus a mid-sentence restart; portion is "whole bag" with no gram weight for the Kirkland SKU

**P158** (stt)  
> chip hotel bowl with double chicken white rice black beans no wait pinto beans and guac

"chip hotel" is Chipotle and the bean line self-corrects, so the resolver must drop black beans and keep pinto

**P159** (stt)  
> morning wait was one eighty to this morning

wait/weight homophone and "one eighty to" means 182 lb; a body-weight entry could be parsed as a food quantity

**P160** (stt)  
> premier protein shake the caramel and half a fah gay

"fah gay" is Fage yogurt; half of an unspecified container size gives no denominator for macros

**P161** (stt)  
> a sigh bowl from the place by the gym with granola and banana on top i guess medium size

"a sigh" is acai and the vendor is unnamed, so calories depend on an unresolvable restaurant portion

**P162** (stt)  
> eco's triple zero to of them the vanilla ones

"eco's" is Oikos and "to of them" is a count of 2, so the multiplier hides inside a mangled preposition

**P163** (stt)  
> in and out double double no wait it was 8 cheeseburger only

"8 cheeseburger" mixes a misheard article with a correction; two different menu items compete for one row

**P164** (stt)  
> starbucks venti cold brew with to pumps vanilla and a splash of oat

"to pumps" is 2 pumps of syrup; syrup pumps and a "splash" of oat milk are both unquantified add-ons

**P165** (stt)  
> okay babe i'm coming uh panda express orange chicken plate with chow main

hot-mic speech to his girlfriend prefixes the log, and "chow main" must map to chow mein as a plate side

**P166** (stt)  
> i coast yogurt and honey plus won scoop of that vanilla wei protein

"i coast" is Oikos and "wei" is whey; a generic scoop has no brand, so protein grams are a guess

**P167** (stt)  
> instant rah men the spicy chicken won and 8 egg cracked in it

"rah men" splits ramen into two words and "8 egg" is one egg misheard as eight

**P168** (stt)  
> for lunch yesterday chicken and rice like every day and to bananas comma also a protein shake

spoken "comma" becomes a token, and "yesterday" must shift the diary date on an after-midnight log

## Not a food log at all

**P169** (speech)  
> how much protein do i have left today

Read query with no food entity; resolver may invent a protein-named item row

**P170** (typed)  
> 181.4

Bare number could be parsed as a portion or calories instead of a pounds weigh-in

**P171** (speech)  
> how many calories was that shake

Question about an existing row, but 'that shake' looks like a Tier-0 saved-meal alias and may re-log it

**P172** (speech)  
> show me yesterday

App navigation command; the diary-date shift is the payload, not a food, so a log write would create an empty row

**P173** (speech)  
> trying to hit 180 by december

Goal statement; 180 could be misread as grams of protein or a weight log for today

**P174** (stt)  
> uh so like whats my protein at right now cause i think i had the fair life already but i dont remember if i logged it or not

Question plus an uncertain past item; risks double-logging fairlife when the real ask is a dedupe check

**P175** (speech)  
> wait did i already log the chicken and rice

Existence check phrased with a food; wait/weight homophone plus a food name invites a duplicate insert

**P176** (typed)  
> nvm

Two-letter cancel token with no nutrition content; may become a food-name lookup that fuzzy-matches something

**P177** (typed)  
> hows my week looking

Aggregate report request with no date range or item; no macros to write

**P178** (stt)  
> whats the protein in oh cost triple zero again

Nutrition lookup about mangled Oikos, not a consumption event; would log a yogurt he never ate

**P179** (speech)  
> um

Filler-only transcript; empty intent but nonzero string passes an emptiness guard

**P180** (speech)  
> delete the last thing i added

Destructive edit command; 'last thing' has no id and the pipeline has no food to resolve

**P181** (speech)  
> gonna try to keep it under like 2200 today

Calorie-budget intent; 2200 could be written as a row's calorie value

**P182** (stt)  
> i weighed 179 8 this morning

Split decimal from STT reads as two numbers, so 179.8 lb may land as 1798 or as a quantity

**P183** (stt)  
> why does it keep saying chip hotel bowl when i say burrito

Bug report naming a mangled Chipotle item; alias-learning could bind burrito to the wrong food

**P184** (stt)  
> is guest bar actually 20 grams or is that marketing bs

Skeptical nutrition question with brand plus grams; the 20 g could be logged as a protein value

## Nasty edges a real person still says

**P185** (speech)  
> uh nothing yet today just water like a bunch of water thats it

Zero-food log: pipeline may invent a water row with 0/0/0/0 or an empty meal instead of recording no intake.

**P186** (speech)  
> i didnt finish the chicken and rice like maybe half of it and no fries i gave those to her

Negation scope: "no fries" must suppress a row while "half" scales only the chicken and rice.

**P187** (stt)  
> split a chip hotel bowl with her so half a bowl i guess chicken and to tortillas no wait won tortilla

"chip hotel"=Chipotle plus "to"/"won" as digits, a spoken self-correction, and a 50% split all multiplying the same portion.

**P188** (speech)  
> just black coffee and a vitamin and some gum thats all so far

Three near-zero-macro items; resolver may attach USDA default calories for coffee or drop the whole log as unloggable.

**P189** (stt)  
> had like for beers at joes last night maybe five i lost count and a shot of whatever

"for"=4 contradicted by the later 5, unnamed shot, and alcohol grams that map to none of the four macro columns.

**P190** (speech)  
> ate half the burrito at like nine and then the other half at one am its the same burrito dont log it twice

One food across two sittings crossing midnight: needs two half rows on the right diary dates, not a duplicated whole.

**P191** (speech)  
> ok so yesterday i had two premier proteins and a bowl of oikos with granola and then chicken and rice for lunch like a big plate and a coupla eggs later and instant ramen at like midnight and half a bag of kirkland trail mix and i weighed 181 point 4 in the morning

Six foods, a bodyweight, and a "yesterday" date shift in one breath: 181.4 must not become a food row and every item must move to the prior diary date.

**P192** (typed)  
> 2 eggs 1 fairlife shke half a bagle 🙃 no butter no cream cheese

Emoji token mid-list plus two trailing negations that must remove condiments rather than add butter and cream cheese rows.

**P193** (speech)  
> the ramen i only drank the broth i didnt eat the noodles

Partial consumption of a composite food: macros must come from broth alone, but the ramen food row has no per-component split.

**P194** (stt)  
> she 8 most of the pizza i had like to slices maybe three of the pepperoni won

"8"=ate, "to"=two, "won"=one, and only my 2-3 slices count while "most" belongs to someone else.

**P195** (typed)  
> havent eaten all day just diet coke n water. 3 cans

"3 cans" could attach to the diet coke, the water, or both, on top of an explicit no-food-today claim.

**P196** (stt)  
> won guest bar but i threw like a third of it in the trash it was nasty

"guest"=Quest and "won"=1, and the consumed amount is two thirds, so the bar's macros need a 0.67 multiplier.

**P197** (stt)  
> won eighty to point for this morning and i havent had anything yet

Weight digits arrive as words ("won eighty to point for"=182.4) with no food at all, so the parser must write a weight and zero rows.

**P198** (typed)  
> 3 whiskey cokes n a hard seltzer idk stopped countin

Mixed drinks with unstated pour size and an unbranded seltzer; alcohol calories have no protein/carb/fat home.

**P199** (speech)  
> no shake today skip it i just had eggs

Mentions the saved-meal shake only to deny it; naming a tier-0 meal must not create its row, and the egg count is missing.

**P200** (typed)  
> ignore the chickfila i logged earlier i never actually got it

Retraction of an existing diary row rather than a new log; needs a delete and needs "earlier" resolved to a specific row id.
