# PARA KAY CODEX — reseal request (2026-08-09)

**Mula kay Claude (strategy/evidence lane), sa utos ng operator.**

## Ano ang hinihiling

Mag-rebuild/reseal mula sa pinakabagong `main` (**`f716b82`** o mas bago) para sa susunod na generation. Kapag tumakbo ang susunod na RTH census, makikita na natin sa wakas ang tunay na admission rejection reason.

## Bakit

- Ang #996 (merged sa `f716b82`) ay nagdadagdag ng **raw-reason sampling** sa admission census — naka-integrate na sa typed-prefix helper ninyo mula #998: ang helper pa rin ang nagno-normalize; ang sampler ay tumatakbo **lang** kapag ang helper ay bumagsak sa purong fallback (`captured_paper_admission_rejected`) — ang klaseng ganap na bulag pa rin. Bounded ito (top-8 distinct, 120 chars, control-chars tinanggal) at lumalabas sa WARNING line + sa sidecar (`raw_samples` key sa `%TEMP%/chili_admission_census.jsonl`).
- Konteksto: noong 08-06 gabi, **17,008 admission calls, 0 admitted, LAHAT naka-mask sa fallback**. Noong 08-07 (build 9294202), gumana ang capture (r137, 246 events) pero **ni isang admission call ay wala** — may hinto sa pagitan ng selection at admission na sa inyong runtime ang lokasyon. Ang dalawang tanong na ito ang haharangin ng parehong reseal na ito + ng inyong selection→admission na pagsisiyasat.

## Kalakip na datos (canonical root, bago)

`D:\CHILI-Docker\chili-data\replay_batch\` — **canon v3 @ fbfa7f2** (21/22 windows, net −1,416.20; UPC excluded 3× timeout). Kasama ang scale-grid attribution (VTAK: grid = −64.28 net) at ang ROI map. Ang v2 ay nasa `archive/2026-08-02_v2_6686a40/`.

— Claude
