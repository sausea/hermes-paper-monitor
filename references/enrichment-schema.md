# Enrichment Queue Schema

`discover` writes one JSON object per line into `runtime/staging/enrichment-*.jsonl`.

Each object already contains source metadata and the raw abstract. Enrichment should add or improve only these fields:

- `dedupe_key` or `paper_id`: keep at least one stable identifier
- `zh_summary`: concise Chinese summary based on abstract or full text
- `affiliations`: list of author affiliations; preserve order when possible
- `notes`: optional notes such as `"pdf restricted"` or `"affiliation inferred from publisher page"`

## Recommended Summary Style

- Use 2-4 Chinese sentences.
- State problem, method, and main contribution.
- Do not invent numbers, datasets, or conclusions not present in the source.

## Recommended Affiliation Style

- Prefer canonical institution names.
- Use a list of strings.
- Leave the field empty if the source is ambiguous, and explain the gap in `notes`.

## Minimal Example

```json
{
  "paper_id": "3db22fd6dfc7a5a9",
  "dedupe_key": "doi:10.1000/example",
  "zh_summary": "本文针对车辆组合导航中的状态估计问题，提出一种结合视觉与惯性观测的鲁棒框架。方法强调在复杂运动和遮挡条件下维持轨迹连续性，并通过实验展示了较好的定位稳定性。",
  "affiliations": [
    "State Key Laboratory of Information Engineering in Surveying, Mapping and Remote Sensing, Wuhan University",
    "School of Automation Science and Electrical Engineering, Beihang University"
  ],
  "notes": "affiliations inferred from publisher metadata"
}
```
