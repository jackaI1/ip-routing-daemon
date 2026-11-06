# Routing event log

The reporter accepts UTF-8 JSON Lines, one JSON object per line. Every input file is one independent run. Milliseconds (`t_ms`) are measured from that run's clock; the supplied simulator uses a deterministic clock rather than wall time. Separate process logs must be ordered and combined into a single run before reporting one network-wide convergence time. Do not pass each router's partial log as if it were a full run.

All events need `event` and `t_ms`. Time is a nonnegative integer from 0 through 999999999999, and accepted events must have nondecreasing times. Equal timestamps are allowed. Required text fields contain 1 to 200 characters without control characters. JSON booleans, nulls, arrays and objects are not valid field values. Extra fields are ignored, allowing a producer to include packet IDs or other context.

| `event` | Other required fields | Meaning |
| --- | --- | --- |
| `link_failure` | `failure_id`, `link` | A connection failed. ID must be unique within this file. `link` is a readable label such as `B-D`. |
| `routes_converged` | `failure_id` | All observed routing tables match the surviving topology. Must reference an unfinished failure in this file. |
| `packet_drop` | `node`, `reason` | One packet was dropped. Each line counts once. |
| `malformed_packet` | `node`, `reason` | One packet was rejected as malformed. |
| `packet_delivered` | `node` | One packet reached its destination. |
| `test_result` | `name`, `status` | A scenario check completed. Status is `pass` or `fail`. Repeated names remain separate observations. |

For example:

```json
{"event":"link_failure","t_ms":100,"failure_id":"f1","link":"B-D"}
{"event":"packet_drop","t_ms":110,"node":"A","reason":"no_route"}
{"event":"routes_converged","t_ms":135,"failure_id":"f1"}
{"event":"test_result","t_ms":136,"name":"alternate_path","status":"pass"}
```

This reports a 35 ms convergence time, one dropped packet and one passing check. Test results share the same clock and stream as routing events; this is not a parser for arbitrary unittest or TAP console output. Convergence is a producer observation, not something inferred from silence or the next delivered packet. A failed check does not change the measured convergence time.

## Rejected lines

- `invalid_json`: JSON is broken, or the top-level value is not an object.
- `invalid_event`: an unknown event, a missing field, or an invalid field value.
- `out_of_order`: the timestamp is earlier than the last accepted event.
- `duplicate_failure`: a previously used failure ID, even if it already converged.
- `orphan_convergence`: a convergence event without a matching failure.
- `duplicate_convergence`: a second convergence event for an already completed failure.
- `line_too_long`: a record exceeds 65,536 bytes, excluding its newline.

Rejected records do not advance the clock or change failure/packet/test counts. Time-order validation happens before failure-ID validation, so a record with both defects is reported as `out_of_order`. Blank lines count as invalid JSON. A complete JSON object without a final newline is accepted. After an oversized record the parser resumes at the next newline, keeping its read buffer bounded. Reports include counts by rejection reason and up to 20 line-numbered examples per input, without echoing raw corrupt content.

## Report and exit behavior

The text report groups each input separately. The CSV has one header and carries the source on every row. `summary` rows count accepted events, rejected lines and delivered packets; `convergence` rows contain start, end and duration; `packet` rows group category and reason; `test` rows preserve individual results; `rejected` rows count each rejection reason; `diagnostic` rows give sample line numbers. Empty convergence cells mean recovery was not recorded, rather than zero milliseconds.

Both report paths are required. Their parent folders must exist. The CLI rejects duplicate inputs and collisions between inputs and outputs, including aliases. It reserves absent destinations and compares filesystem identities, so case and Unicode-normalization aliases are caught too. It removes only its own unused reservations after errors, and stages each output in the destination folder before replacing it. The two final renames are separate operations, so an I/O failure during replacement can leave only one new report; check the exit status before consuming the pair. UTF-8 output uses quoted CSV fields, doubles embedded quotation marks, and prefixes formula-like values with an apostrophe.

Default mode exits 0 after writing a report even if log records were rejected. `--strict` exits 2 after writing when any file has no usable events, any rejected lines, an unfinished failure, or a failed scenario check. A run without failures or scenario checks is allowed: `none observed` and `none recorded` do not imply those events occurred. Packet losses themselves do not make strict mode fail, because losses are expected observations in failure experiments. Usage and file errors exit 1.

## Linux namespace demo

`sudo python3 lab/topology.py` produces `build/lab/events.jsonl` using measured monotonic elapsed milliseconds. The lab records the link-down action and waits until an independent weighted-path observer finds that every route at all four live routers matches the surviving topology. It then records `routes_converged`, followed by ping checks and delivered echo replies. Run the reporter against that one observation stream, not the routers' separate debug logs. Debug logs contain rate-limited drop messages in another schema and are not a complete loss-accounting stream. The simulator's deterministic event stream and reporting behavior are unchanged.
