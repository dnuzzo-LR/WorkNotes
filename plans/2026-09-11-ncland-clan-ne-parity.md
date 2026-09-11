# ncland: porting the remaining clan NE types

Tracking doc for bringing ncland to CLI parity with clan. Started 2026-09-11.

## Where things stand

| | count |
|---|---|
| dtypes with a clan card (`HAS_CLAN_CARD`) | 63 |
| in the ncland registry | 28 |
| remaining | 35, of which 6 are out of scope (below) |
| portable target remaining | 29, across 16 families |

Shipped so far: PR #7409 (Ciena Z Series 155, Cisco 4200 103, and four NE types
that never loaded), then branch `ncland-more-ne-types` (203, 258, 172, then the
setupIOSConnection three: 70, 97, 217).

## Six of the 39 cannot be ported from clan

`setupConnection` defaults to `setupTSS5Connection` (clan.c L955), so a dtype
that falls through the dispatch chain gets a Nokia 1850 TSS-5 login attempted
against it. These six never appear in clan.c at all (T300's single hit is a
simulator-name mapping, not a driver):

| dtype | NE type | reachable via |
|---|---|---|
| 71 | FUJITSU 1FINITY T600 | restclan |
| 108 | CIENA COHERENT ELS | restclan |
| 110 | ADVA FSP 3000 C | netclan |
| 187 | FUJITSU 1FINITY T300 | restclan |
| 261 | CIENA O-NID | netclan + restclan |
| 264 | CIENA WAVESERVER E-SERIES | netclan + restclan |

Customers want CLI where the equipment supports it, but there is no clan driver
to port for these. They need new drivers written against the real NE CLI, which
means NE documentation or lab access — not derivable from the codebase.

**Decision 2026-09-11 (Dan): out of scope for now.** ncland is not losing
anything clan has, because clan does not drive these over CLI either. Revisit
only if a customer asks for CLI on one of them specifically, and then as new
development against the equipment rather than as a port.

**So the portable target is 33 dtypes across 17 families.**

## Done

| family | dtypes | branch |
|---|---|---|
| `setupZSeriesConnection` | 155 | PR #7409 |
| `setupIOSConnection` | 103, 228, then 70, 97, 217 | #7409 / `ncland-more-ne-types` |
| `setupNew1830Connection` | 207, then 203, 258 | `ncland-more-ne-types` |
| `setupTSS5Connection` | 171, then 172 | `ncland-more-ne-types` |
| `setupSmartOpticsDCPConnection` | 72, 75, 88, 93 | `ncland-more-ne-types` |

Already present before this effort: `setupCienaRLSConnection` (89),
`setupWaveServerConnection` (252-254), `setupFuji1FinityConnection` (119-121),
`setupAdvaXG400Connection` (101, 137), `setupNokiaFxConnection` (104),
`setupPSI2TConnection` (156, 158, 215).

## Remaining families, largest first

| clan driver | dtypes | NE types |
|---|---:|---|
| `(none - generic/unhandled)` | 6 | 71 FUJITSU 1FINITY T600<br>108 CIENA COHERENT ELS<br>110 ADVA FSP 3000 C<br>187 FUJITSU 1FINITY T300<br>261 CIENA O-NID<br>264 CIENA WAVESERVER E-SERIES |
| `setupCiscoConnection` | 4 | 91 CISCO ONS 15454<br>153 CISCO ONS 15310-MA<br>188 CISCO ONS 15310-CL<br>249 CISCO NCS2000 |
| `setupSmartOpticsDCPConnection` | 4 | 72 SMARTOPTICS DCP-M40<br>75 SMARTOPTICS DCP-M8<br>88 SMARTOPTICS DCP-2<br>93 SMARTOPTICS DCP-R-9D |
| `setupBTI78XXConnection` | 3 | 124 JUNIPER NETWORKS BTI7801<br>125 JUNIPER NETWORKS BTI7802<br>126 JUNIPER NETWORKS BTI7814 |
| `setupCienaCESConnection` | 3 | 98 CIENA 8112<br>148 CIENA CES<br>149 CIENA 8700 |
| `setupInfineraGrooveConnection` | 3 | 87 NOKIA 1830 GX G40 SERIES<br>238 INFINERA DTN/DTN-X<br>251 INFINERA GROOVE G30 |
| `setupNCS1002Connection` | 3 | 138 CISCO NCS1001<br>139 CISCO NCS1004<br>250 CISCO NCS1002 |
| `setupCoreDirectorConnection` | 2 | 240 CIENA 5410/5430<br>242 CIENA CORE DIRECTOR |
| `setupTSS100Connection` | 2 | 173 NOKIA 1850 TSS-15<br>174 NOKIA 1850 TSS-100 |
| `setup1678Connection` | 1 | 160 NOKIA 1678 MCC |
| `setupInfineraDtxItmConnection` | 1 | 96 INFINERA XTM |
| `setupLUConnection` | 1 | 122 NOKIA 1675 LambdaUnite |
| `setupMarvellTeralynxConnection` | 1 | 94 MARVELL TERALYNX |
| `setupNT6500DWDMConnection` | 1 | 233 CIENA 6500 DWDM |
| `setupNokiaWaveliteConnection` | 1 | 259 NOKIA WAVELITE |
| `setupSymmetricom2700Connection` | 1 | 262 SYMMETRICOM TIMEPROVIDER 2700 |
| `setupTSS320Connection` | 1 | 185 NOKIA 1850 TSS-320 |
| `setupTSS3Connection` | 1 | 205 NOKIA 1850 TSS-3 |
## Method, per family

One YAML per dtype (prompts and paging genuinely differ within a family), one
Lua per family, ported from the family's `setup*Connection`. Where several
dtypes share a login but differ after it, use one module with a post-login
table keyed on `ne_type` — the shape `ios_router.lua` uses, mirroring clan's
own dtype switch.

Per family, check rather than assume:

1. dtype-specific branches inside the `setup*Connection`
2. prompt macros in `clan_libssh.c` (PROMPT1 vs PROMPT2 often differ)
3. keepalive command (clan.c ~L1750-L1790) and cadence (the
   `pingInterval = 600` list at L409-L414; absent means the 60s default)
4. paging token and answer (L2298 picks the drain; L2369-L2381 picks space vs
   newline)
5. whether the dtype is in `nclan_seed`'s `dtype_uses_es64` set, which decides
   whether transport comes from the es64 record or the YAML default

Tests per family: registry load with zero skips, and the real Lua driven
through its login over a socketpair.

## Open question — revisit when the port is done

**`IS_CISCO_ETH_MGMT` transport (dtypes 91, 153, 188, 249).** Unlike most NE
types this family is NOT in `nclan_seed`'s `dtype_uses_es64` set, so nothing
overrides the yaml at runtime — whatever `ne/cisco_ons_base.yaml` says is final.
clan chooses ssh vs telnet from the global `SSHPath`, which ncland does not
model at all. The port ships `protocol: [telnet, ssh]` because the login
dialogue opens on a bare "Password:" with no user name, which is the telnet
console shape.

To settle (Dan flagged 2026-09-11):
- how these NEs are actually reached in the field — telnet or ssh
- whether ncland should grow an SSHPath-equivalent, or whether these dtypes
  should join `dtype_uses_es64` so the es64 record supplies transport like it
  does for every other family
- the same question applies to any other family that turns out to be absent
  from `dtype_uses_es64`; worth auditing the full set once the port is complete

## Known gaps carried forward

- Real-NE credentials are never exercised by the simulators —
  `warehouse_open_conn_by_ne` skips the es64 lookup for sim addresses, so the
  password and `p_enable` branches run with empty values.
- ncland does not send the SNMP trapdest that clan sets up for the 1830 family;
  a deliberate call, documented in `ne/nokia_psi_l.yaml`.
- Juniper's `display xml` / rpc-reply handling (`clanCheckXmlRpcReply`) has no
  ncland equivalent.
- **Prompt capture takes the last line of the expect span**, which includes
  whatever the previous expect left on that line (the space after
  "Password:", say). For NEs whose prompt pattern admits spaces that residue
  gets baked into the rebuilt regex. Fixed in `smartoptics_dcp.lua` by
  trimming; `ciena_rls.lua` and `nokia_1830_pss.lua` use the same idiom with
  equally space-tolerant patterns and are still exposed. The real fix is
  GAPS.md Gap 2 -- an accessor for the matched text alone.
- **clan's prompt rebuilds paste the live prompt in unescaped.** Confirmed
  broken for SmartOptics, whose prompts contain brackets; the port escapes.
  Worth checking the other `adapt_from_actual` NEs against their real prompt
  shapes.
