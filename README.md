# loadslack

A sidechain compressor for AI compute.

Three claims, stacked. **One:** there is waste in the token stream — duplicate
work, dead context, redundant retries, over-allocated output — that costs
energy and buys nothing. **Two:** removing waste is not a tradeoff, so a driver
that removes only waste cannot reduce performance. **Three:** nobody will
believe claim two on your word, so the driver proves it about itself,
continuously, forever.

The third claim is the product. The first two are the giveaway.

Zero dependencies. Python 3.9+.

## Install

```bash
pip install loadslack                       # once published
pip install git+https://github.com/rayj3088/loadslack.git   # from source, today
```

No wheels to audit, no transitive dependencies, nothing to review but the
Python standard library and this repo.

```bash
loadslack waste      # rot elimination + live proof, free vs paid tier
loadslack demo       # a simulated ISO curtailment day
```

## Put it in front of your traffic

```bash
loadslack serve --upstream https://api.openai.com \
               --cache /var/lib/loadslack/cache.db \
               --signal-file /var/run/grid.json
```

Then change one line in the application:

```python
client = OpenAI(base_url="http://127.0.0.1:8787/v1")   # python
```
```js
const client = new OpenAI({ baseURL: "http://127.0.0.1:8787/v1" });  // node
```

Node and every other language need **nothing installed** — the driver is a
process, not a library, and speaks the OpenAI-compatible wire format. There is
no npm package to publish because there is nothing for it to do.

Optional headers: `X-LoadSlack-Site` groups traffic for per-pipeline statistics
(defaults to the model name); `X-LoadSlack-Key` overrides holdout assignment.

Streaming works. Chunks relay as they arrive, TTFT is measured from the first
byte upstream, and a cache hit on a streaming request is re-emitted *as* a
stream — buffered and streamed calls share one cache.

Run more than one worker and pass `--cache`; a per-process cache halves its own
hit rate the moment there are two of you.

## The bypass button

A mix engineer does not trust a compressor's numbers. They hit bypass, over and
over, and listen for whether they can tell. If you can hear it working, it's
wrong.

So the bypass runs permanently. A deterministic ~3% slice of traffic passes
completely ungoverned, and the driver compares the two populations live:

```
Net perceived latency change -36.4 ms (95% CI -55.2 to -17.6),
n=1361 governed vs 39 ungoverned. 0 identity failures.
```

What is claimed, and what is not:

| | claim | how |
|---|---|---|
| **Latency** | claimed | live holdout, 95% CI, three-valued verdict — thin data returns `INSUFFICIENT_DATA`, not `PASS` |
| **Identity** | claimed, scoped | eliminated work is byte-identical to what the model produced |
| **Quality** | **not claimed** | not cheaply measurable. The defence is structural: no mechanism alters an answer. Anyone claiming their middleware is *proven* not to degrade quality is selling you something. |

## What gets removed

Quality-neutral by construction, not "usually fine":

- exact duplicates, with volatile tokens (timestamps, uuids) normalised out
- in-flight collapse — an identical call already upstream
- near-duplicates, **safety-gated** (below)
- dead context — turns no later turn ever refers to
- output over-allocation — `max_tokens: 4096` when p99 is 190, which reserves
  real KV capacity on the serving side
- cache-hostile prefixes — *reported, not fixed*. One timestamp at the top of a
  system prompt and the provider's prefix cache never hits, forever, and
  nothing is broken so nobody notices. That fix lives in the customer's code.

### The safety gate

A false cache hit is a silent wrong answer, which is worse than a wasted call.
Matching is gated on the **symmetric difference** of tokens, not on similarity
— two prompts can be 99% identical and mean opposite things.

Refused if the differing tokens contain a negation, a numeral, or anything on
the caller's never-cache list. In the demo that refuses 1,480 matches. Those
are savings deliberately not taken, and they are counted in the report, because
a ledger that only shows wins is not a ledger.

## The grid coupling

Grid stress is the detector input. It modulates exactly one thing: **effort** —
how hard the engine looks. Wider near-duplicate windows, deeper pruning, longer
coalescing. It never swaps a model, never trims reasoning, never touches
quality.

```
free tier,  calm or stressed grid   effort 0.20   20.9% of calls eliminated
paid tier,  stress 0.92             effort 0.88   57.6% of calls eliminated
```

## The latency invariant

A cache hit *returns* time to the user. Coalescing *spends* it. The ledger
tracks the balance and refuses to spend from a deficit, so you only ever spend
milliseconds you already earned. `invariant_holds: true` is checkable at
runtime.

## Free vs paid

| free | paid |
|---|---|
| rot elimination | grid sidechain |
| holdout verifier | per-model calibration bundle |
| latency ledger | countersigned receipts |
| waste report | |

A lapsed licence **degrades, it does not break**. The driver keeps eliminating
waste and keeps proving it did no harm. It simply stops following the grid.

Why a subscription and not a purchase: the waste profile is model-specific and
models turn over constantly. Every release changes context economics,
tokenizer behaviour, prefix-cache granularity. What recurs is not access to a feature — access can be
copied — it is **calibration that decays**.

## Using it

```python
from loadslack import Sidechain

sc = Sidechain()                                   # free tier
t = sc.before("support-bot", prompt, messages=msgs, max_tokens=800)
if t.served:
    return t.response                              # duplicate work, answered locally
resp = call_upstream(**t.request)                  # pruned / right-sized
sc.after(t, resp, output_tokens=n, ttft_ms=ttft)

print(sc.datasheet())                              # the live proof line
```

## Operating it

| endpoint | what it is for |
|---|---|
| `GET /proof` | the live claim, in one line, for a datasheet or a review |
| `GET /state` | effort, elimination counters, cache size, licence tier |
| `GET /metrics` | Prometheus: effort, eliminations, refusals, TTFT delta, verdict |
| `POST /signal` | push grid stress or a curtailment order (or wire OpenADR to it) |

The verdict is exported as a labelled gauge, so
`loadslack_verdict{verdict="FAIL_SLOWER"} == 1` is an alert you can page on. A
driver that can page you when it is failing is a different object from one that
asks you to trust it.

## The quality-tradeoff ladder (optional, separate)

The original `Governor` path is still in the package for facilities under a
hard curtailment order that rot elimination alone cannot satisfy. It *does*
trade quality for joules, which is why it is opt-in and separate.

### Why a compressor and not a circuit breaker

A breaker is a step function: on, off, application dies. A resistor is a curve.
`loadslack` implements a real compressor — threshold, ratio, soft knee, attack,
release, hold — because the failure modes of naive throttling are the failure
modes of naive audio compression: pumping, chatter, and holes you can hear.

Below threshold it does nothing at all. In most regions, most hours, that is
the correct behaviour, and it is what makes the driver safe to leave installed.

## The ladder

Reduction is met by engaging stages in ascending order of perceptual cost, at
fractional depth, composing **multiplicatively** (two stages that each remove
20% remove 36% together, not 40%).

| rung | stage | prior savings | what the user feels |
|---|---|---|---|
| 0 | `batch_coalesce` | 22% | nothing below ~300 ms |
| 1 | `cache_align` | 10% | nothing; output identical |
| 2 | `sample_depth` | 18% | small accuracy cost |
| 3 | `reasoning_trim` | 30% | bites on hard prompts only |
| 4 | `model_downshift` | 55% | measurable quality change |
| 5 | `defer` | 98% | latency, not quality |

Perceptual cost is **class-dependent**. For a nightly batch job, waiting six
hours costs nobody anything while a smaller model changes the output, so
`defer` sorts to the *front* for `BATCH`. For `INTERACTIVE` it is permanently
out of reach.

Ceilings by class, and the maximum reduction each can reach:

| class | ceiling | headroom |
|---|---|---|
| `interactive` | 0.10 | **29.8%** — from invisible rungs only |
| `near_realtime` | 0.30 | 59.7% |
| `background` | 0.65 | 99.6% |
| `batch` | 1.00 | 99.6% |

If the ordered reduction exceeds what a class can deliver, the plan reports
`shortfall` and the receipt records it. The driver will not quietly break your
product to hit a number.

## Wiring it in

**As a proxy.** Point any OpenAI-compatible client at it:

```
loadslack serve --upstream https://api.openai.com --signal-file /var/run/grid.json
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
```

Tag traffic with `X-LoadSlack-Class: interactive | near_realtime | background | batch`.
Untagged traffic defaults to `interactive` and is therefore never deferred.

**As a library.**

```python
from loadslack import Governor, RequestClass, FileSource

gov = Governor(source=FileSource("/var/run/grid.json"))
d = gov.admit(model="frontier", request_class=RequestClass.BACKGROUND,
              input_tokens=4000, output_tokens=800)
if d.deferred:
    ...                      # durably queued; task id on d.task_id
else:
    time.sleep(d.coalesce_ms / 1000)
    call_upstream(**d.apply(kwargs))
```

**Signal sources.** `FileSource` (a JSON file any cron job can write),
`HttpPollSource` (bring your own mapper for WattTime, Electricity Maps, an ISO
feed), `WebhookSource` (POST `/signal`; wire an OpenADR 3.0 VEN or a DERMS to
it), `CompositeSource` (max stress wins, any live hard order wins).

A hard curtailment order — `shed_mw`, `target_mw` or `reduction_fraction` —
bypasses the curve entirely and puts the unit into limiter mode on a fast
attack, because when the utility says shed 40 MW in ten minutes that is not a
suggestion.

## Side effects

Deferring a call is easy. Deferring an agent loop that already sent an email is
not. The queue uses a two-phase intent log:

```python
if q.reserve(task_id, "send-invoice-881", "email"):
    send_email(...)
    q.commit(task_id, "send-invoice-881", {"message_id": mid})
```

`reserve()` returns `False` if that key already committed — your idempotency
check on resume. Intents reserved but never committed are in an unknown state:
they are **not** replayed and **not** dropped. They surface in
`loadslack queue --orphans` for reconciliation, because a human or a downstream
idempotency check is the only correct arbiter. An honest unknown beats a
confident duplicate.

## Receipts

The mechanism is worth having but is not defensible. What a facility under a
curtailment tariff needs the morning after an event is an artifact.

Every decision is appended to a SQLite hash chain — each row's digest covers
the previous row's — so altering or deleting any row invalidates everything
after it. The head is HMAC-signed with a key the operator holds.

```
loadslack verify --ledger receipts.db --report-since 1789900000
```

## What is not calibrated

Read this before anyone puts a number from this tool in a filing.

Nobody can currently tell you joules per token for a hosted frontier model.
Providers do not publish it, and token count is a poor proxy: reasoning tokens,
MoE sparsity, batch occupancy, speculative decoding and prefix-cache hits each
swing the real figure by a large factor.

So every energy figure is a **band**, not a number, and every constant in
`energy.py` and the savings column above is a **prior**, not a measurement.
Until `EnergyModel.calibrate()` has been run against a metered rack, receipts
carry `calibrated: false` and an explicit warning, and no regulator should
accept them as evidence.

The calibration path: meter a rack, drive a known token mix through it, call
`calibrate()` with the measured Wh. The band tightens and the warning clears.

## Failure posture

- **Fail open.** Dead or stale signal feed → stress 0 → no compression. A
  middleware that throttles on stale data gets uninstalled after its first bad
  afternoon. Override with `fail_stress` if your tariff makes a missed
  curtailment worse than an unnecessary one.
- **Interactive is sacred.** No hard order can defer or downshift a
  human-facing request. The shortfall is reported instead.
- **No silent degradation.** Every decision logs what it did and what it could
  not do.

## Support & Sponsorship

LoadSlack is open-source software built to reduce AI compute overhead and promote sustainable energy practices. If this project helps you optimize your infrastructure or save energy, consider supporting ongoing development!

* 💳 **PayPal**: [paypal.me/rayj3088](https://paypal.me/rayj3088)

Your support directly fuels research, model benchmarks, and open-source infrastructure tools.

## Licence

Apache-2.0.
