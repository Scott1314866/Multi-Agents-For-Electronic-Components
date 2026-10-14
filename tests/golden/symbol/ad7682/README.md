Regression inputs from the supplied AD7682/AD7689 datasheet (WJ1235149, pages 11–14).
`mineru_pin_tables.json` preserves the actual OCR output, including `1,20`, `4,5`,
the multi-row model headings, continued tables, `INO`, and the wrapped EPAD row.
The WLCSP table is included to check that another package cannot be merged.

`reference_pins.json` transcribes the user's verified `引脚图.png` supplied on
2026-10-09: pins 1–10 on the left, 21–11 on the right, IN0 at 16 and EP at 21.
The raw datasheet label EPAD and the reference label EP denote the same exposed pad.
The workflow test checks automatic, evidence-backed resolution of INO/IN0 and
EPAD/EP against this reference, without a manual-review interrupt. Substantive
conflicts and unverified OCR differences are covered separately.
