# v1.5.9.30 — CTA Two-Strategy Benchmark

## Segment benchmark
- Add a dedicated **So sánh hiệu năng** action inside CTA Detector.
- Compare exactly two strategies on the same bounded video segment:
  1. One pass at 2.0 seconds per sampled frame.
  2. Two passes: first pass at 4.0 seconds per frame, then a 1.0-second recheck around regions signalled by first-pass candidates.
- Both profiles use the same balanced OCR and independent graphic Subscribe recognition; only the sampling strategy changes.
- Report elapsed time, pass-one/pass-two sample counts, total samples, candidates, average confidence and OCR timeouts.
- Let users manually label each candidate as a true CTA or false positive. When the known number of real CTA events in the segment is supplied, calculate Precision, Recall and F1. Confidence is not accuracy.
- Benchmark results remain separate from the project's main CTA results/cache. Nothing is auto-selected or cut.
- Add a detector end-time boundary so the engine stops reading at the requested end.
- Document the recall limitation: a CTA that produces no first-pass cue may not be rechecked in the second pass.

## Existing CTA processing preserved
- Keep the v1.5.9.29 full-video default cadence and OCR/graphic recognition behaviour.
- Keep Batch CTA sequential processing and manual review before cutting.
