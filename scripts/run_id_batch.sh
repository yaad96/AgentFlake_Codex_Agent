#!/usr/bin/env bash
# Run the 41 ID containers sequentially, one pass@k sweep each.
# Resumable: a container that already has a completed run is skipped.
# PRUNE_ZIPS=1 deletes each subject archive once its run completes, so peak
# disk is one container's working set instead of all 38 archives (6.3 GB).
set -uo pipefail

REPO="${REPO:-$HOME/AgentFlake_Codex_Agent}"
PY="$REPO/.venv/bin/python"
RUNNER="$REPO/AF_Codex_Agent/agentic/run_agentic_pass_at_k.py"
CSV="$REPO/AF_Codex_Agent/test_config.csv"
DATA="$REPO/AF_Codex_Agent/data"
RUNS="${RUNS:-1}"
MINGB="${MINGB:-10}"
LOGDIR="${LOGDIR:-$HOME/codex_id_logs}"
mkdir -p "$LOGDIR"

CONTAINERS=(
commonslang1163e17testReflectionHashCodeExcludeFields
jsonschemacore7dbae50multipleSchemaDepViolation
scimonoscimonoclient7487ec5testCreateDefaultIdentityFilter3
fastjson97ee7b6test_for_issue5
apollojavaapolloopenapi5344bc4testFindItemsByNamespace
crane4jcrane4jcoreb73311aget
graylog2servergraylog2server27269f2summarizeUsersReturnsListOfUsersIfCurrentUserIsNull
ednjava2d37e22testPrettyPrinting
elideelidecore5c39308testHiddenFields
castlejavaa5e9ef9minimalContextAsJson
idservicecombea50142swagger
idflink1c06b74btable
idhbaseb162d1aserver
idhivestnd1
idjbpmcase1
dubbodubbocommon690d397testGetAllDeclaredAnnotations
ecoschemacatalogstore95ee43btestCorrectRemoveOfVersionWithNoOriginKey
bladebladecoree925deatestAddStatics
avrolangjavaavro7fd098atestRecord
furyfurycore68ca4bftestTraverseExpression
SCB-2692
karatekaratecore935f0a8testBeanConversion
OpenRefinemaina68ba3bserializeListFacet
oktahookssdkjavahooks9187787createUserTest
shenyushenyuadmin6bfb86btestBuildHandle1
graylog2servergraylog2server036bdb5serializePrefixOnly
graylog2servergraylog2serverf169d54serializeInteger
dubbodubbocommon83c466etestGetMetaAnnotations
elideelidecore80439b6writeSingleIncluded
ecoschemacatalogstore95ee43btestRemove22
castlejavaa5e9ef9fullBuilderJson6
bladebladecoreaa32ce9testRouteMatcher
bytebuddybytebuddydepe997263testNonGenericParameter
cloudstackpluginsnetworkelementsopendaylightadec811gsonNeutronPortMarshalingTest1
castlejavaa5e9ef9jsonSerialized18
crane4jcrane4jcore679c3f8process
cloudstackpluginsnetworkelementstungstendf4cd2alistTungstenNetworkTest
adyenjavaapilibraryb8a8de5testPaymentsRequestWithXidAndCavv
nacoscommon2c5c85ctestGetMap3
jerseymediajsonjackson1b99237testDisabledModule
nifinificommonsnifirecord7823156testAliasConflictingAliasValues
)

[[ -x "$PY" ]]     || { echo "ERROR: no venv interpreter at $PY (run setup.sh)"; exit 1; }
[[ -f "$RUNNER" ]] || { echo "ERROR: runner not found at $RUNNER"; exit 1; }
[[ -f "$CSV" ]]    || { echo "ERROR: test_config.csv not found at $CSV"; exit 1; }
KEYFILE="$REPO/AF_Codex_Agent/.openai_api_key"
[[ -s "$KEYFILE" ]] || { echo "ERROR: no key in $KEYFILE"; exit 1; }
KEY="$(head -1 "$KEYFILE" | tr -d '[:space:]')"
code=$(curl -sS -o /tmp/preflight.json -w '%{http_code}' \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"hi","max_output_tokens":16}' \
  https://api.openai.com/v1/responses)
if [[ "$code" != "200" ]]; then
  echo "ERROR: API pre-flight failed (http=$code). Not starting the batch."
  cat /tmp/preflight.json; exit 1
fi
mkdir -p "$DATA"
avail=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc '0-9')
if [[ -n "$avail" && "$avail" -lt "$MINGB" ]]; then
  echo "ERROR: only ${avail}G free under $DATA (need >= ${MINGB}G)."
  echo "       Free space, or lower the bar with MINGB=<n>."
  exit 1
fi
echo "[batch] pre-flight OK (API 200, ${avail:-?}G free, prune_zips=${PRUNE_ZIPS:-0})"
echo "[batch] ${#CONTAINERS[@]} containers, runs=$RUNS, logs -> $LOGDIR"

started=$(date +%s)
declare -i n=0 skip=0 fail=0
for c in "${CONTAINERS[@]}"; do
  n+=1
  if compgen -G "$DATA/$c/run_*/.run_complete" >/dev/null 2>&1; then
    echo "[$n/${#CONTAINERS[@]}] SKIP  $c"; skip+=1; continue
  fi
  free_now=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc '0-9')
  echo "[$n/${#CONTAINERS[@]}] RUN   $c   (${free_now:-?}G free)"
  t0=$(date +%s)
  if "$PY" "$RUNNER" "$c" --runs "$RUNS" > "$LOGDIR/$c.log" 2>&1; then st="ok"; else st="FAILED"; fail+=1; fi
  echo "      -> $st  $(( $(date +%s)-t0 ))s  $(grep -oE 'DONE\. [0-9]+/[0-9]+ runs PASSED[^ ]*' "$LOGDIR/$c.log" | tail -1)"
  if [[ "${PRUNE_ZIPS:-0}" == "1" ]] && compgen -G "$DATA/$c/run_*/.run_complete" >/dev/null 2>&1; then
    zipname=$(awk -F',' -v c="$c" 'NR>1 && $2==c {print $3; exit}' "$CSV")
    if [[ -n "$zipname" && -f "$DATA/$zipname.zip" ]]; then
      rm -f "$DATA/$zipname.zip"; echo "      pruned $zipname.zip"
    fi
  fi
done
echo
echo "[batch] finished in $(( ($(date +%s)-started)/60 )) min  attempted=$n skipped=$skip failures=$fail"
