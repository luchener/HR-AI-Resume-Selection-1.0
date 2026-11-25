export const REPORT_EXPORT_STYLE = `
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;color:#2c394f;font-size:12.5px;line-height:1.65;background:#ffffff}
.page{width:794px;margin:0 auto;padding:26px 30px 30px;background:#ffffff}
.hero{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;background:#f4f7fc;border:1px solid #e3e9f2;border-radius:10px;padding:16px 18px}
.kicker{font-size:10.5px;letter-spacing:.12em;color:#6b7a90;text-transform:uppercase}
h1{font-size:24px;line-height:1.25;color:#1f2c40;margin:4px 0 3px}
.meta{font-size:10.5px;color:#6b7a90}
.hero-score{display:block;text-align:right;min-width:150px}
.hero-score .num{font-size:32px;font-weight:700;line-height:1.15;color:#1d7f5c;margin-bottom:14px}
.hero-score .den{font-size:10.5px;font-weight:500;color:#6b7a90}
.hero-score .rec{display:inline-block;margin-top:0;background:#e7f5ee;color:#1d7f5c;border-radius:999px;padding:3px 10px;font-size:10.5px;font-weight:600}
.hero-score .grade{margin-top:4px;font-size:10.5px;color:#6b7a90}
.verdict{margin:12px 0;padding:11px 13px;border-left:3px solid #3e6fd3;background:#f8fafd;border-radius:0 6px 6px 0}
.verdict .label{font-size:10.5px;font-weight:600;color:#3e6fd3;margin-bottom:3px}
.verdict p{font-size:12.5px;line-height:1.75;color:#33445c}
.verdict .verdict-notice{margin-top:4px;font-size:11px;color:#52627c}
.cards{display:flex;margin:12px 0 2px}
.card{flex:1;min-width:0;border:1px solid #e3e9f2;border-radius:8px;padding:9px 11px}.card + .card{margin-left:8px}
.card .k{font-size:10px;color:#6b7a90}
.card .v{font-size:16px;font-weight:700;color:#1f2c40;margin-top:2px;line-height:1.35}
.card .v.small{font-size:12px;line-height:1.45}
.card .den{font-size:10px;font-weight:500;color:#6b7a90}
.card .s{font-size:9.5px;color:#7a879a;margin-top:2px}
.band{font-size:9.5px;color:#8190a4;margin:4px 0 2px}
h2{font-size:13px;color:#253249;margin:17px 0 7px;padding-left:8px;border-left:3px solid #3e6fd3;line-height:1.3}
section{break-inside:avoid}
.qa{background:#fbfcfe;border:1px solid #e7edf6;border-radius:9px;padding:12px 14px 10px;margin:14px 0}
.qa h2{margin-top:0;border-left-color:#1d7f5c;color:#1f2c40}
.questions{list-style:none;margin:2px 0 4px;padding:0}
.questions li{position:relative;margin-bottom:7px;font-size:12.5px;line-height:1.7;color:#2c394f;padding-left:20px}
.questions .qnum{position:absolute;left:0;top:0;color:#1d7f5c;font-weight:700}
ul.tight{margin:2px 0 4px 17px}
ul.tight li{margin-bottom:4px;font-size:12px;line-height:1.65}
.facts{display:flex;flex-wrap:wrap;gap:7px 20px;background:#f8fafd;border:1px solid #e3e9f2;border-radius:8px;padding:9px 12px;margin-bottom:7px}
.facts span{font-size:11.5px;color:#2c394f}
.facts span b{display:block;font-size:9.5px;color:#6b7a90;font-weight:500;margin-bottom:1px}
table{border-collapse:collapse;width:100%;margin:5px 0 8px;font-size:11px}
th,td{border:0;border-bottom:1px solid #e8edf5;padding:5px 7px;text-align:left;vertical-align:top;line-height:1.55}
th{background:#f4f7fc;color:#4b5c73;font-size:9.5px;font-weight:600;letter-spacing:.02em}
tr:nth-child(even) td{background:#fbfcfe}
td.lab{background:#f7f8fa;color:#66758b;font-size:9.5px;width:62px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 6px}
.chips span{display:inline-block;border-radius:999px;padding:2px 9px;font-size:10px;background:#f1f4f9;color:#4b5c73;border:1px solid #e3e9f2}
.chips .ok{background:#e9f6ef;border-color:#cfeadd;color:#1d7f5c}
.chips .warn{background:#fdf4e3;border-color:#f3e2c2;color:#a06a12}
.chips .bad{background:#fbecef;border-color:#f0cfd6;color:#b23b4e}
.chips .muted{color:#6b7a90}
p.para{font-size:12px;line-height:1.7;margin:3px 0 6px}
.sub{color:#6b7a90;font-size:10px;line-height:1.6;margin:3px 0}
.green{color:#1d7f5c}.red{color:#b23b4e}.purple{color:#995c87}.blue{color:#3e6fd3}
.appendix{background:#fbfcfe;border:1px solid #e7edf6;border-radius:8px;padding:10px 12px;margin-top:14px}
.appendix h2{margin-top:0;font-size:12px;border-left-width:2px}
.appendix p{font-size:10.5px;line-height:1.6;color:#4b5c73;margin-bottom:4px}
.notice{margin-top:14px;border-top:1px solid #e8edf5;padding-top:7px;font-size:9.5px;color:#8190a4}
.empty{color:#8190a4}
@media print{.page{width:auto;padding:8px 10px}body{font-size:12px}.hero{break-inside:avoid}.qa{break-inside:avoid}}
`;

export const REVIEW_EXPORT_STYLE = `
*{margin:0;padding:0;box-sizing:border-box}body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;font-size:14px;line-height:1.8;color:#2c394f;padding:32px 36px;background:#fff;width:780px;margin:0 auto}
h1{font-size:20px;color:#253249;margin-bottom:4px}h2{font-size:15px;color:#3e6fd3;margin:18px 0 6px;border-bottom:1px solid #dce2eb;padding-bottom:4px}
.meta{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0 16px;font-size:13px;color:#65738a}.meta span b{color:#1d7f5c;font-size:22px;margin-right:3px}
table{border-collapse:collapse;width:100%;margin:6px 0 16px;font-size:13px}th,td{border:1px solid #dce2eb;padding:6px 10px;text-align:left;vertical-align:top}th{background:#eaf0fb;color:#3e6fd3;font-weight:600;font-size:12px}
.resume{white-space:pre-wrap;word-break:break-word;background:#fbfcfe;border:1px solid #e5e9ef;border-radius:6px;padding:14px;margin-top:6px;font-size:13px;line-height:1.9}
.notice{margin-top:16px;font-size:11px;color:#8190a4}
@media print{body{padding:12px 16px;width:auto}}
`;


