#!/bin/zsh
# Overnight scenario batches (Kieran, 2026-11-21: "go through 50 different scenarios ... then another 50 ... until the morning").
# Each batch = 50 new fake commits (work/night<N>, seed 40+N, every kind of change, incl. kinds the defaults skip),
# with their real failing tests from the full sqlglot suite. A batch is finished when work/night<N>/DONE exists.
# One heavy job at a time, at the lowest priority. Stops starting new batches at 9 AM.
#   zsh tools/night.sh [first batch number]
cd "$HOME/Desktop/Blast Radius" || exit 1
n=${1:-1}
KINDS="code,code,code,code,class,class,class,comment,comment,fixture,fixture,fixture,delete,delete,test,string,string,table,table,equiv,default,decorator,newattr,base"
while true; do
  h=$(date +%H)
  if (( 10#$h >= 9 && 10#$h < 20 )); then
    echo "$(date '+%F %T') morning: no new batch" >> work/night.log
    break
  fi
  d=work/night$n
  if [[ ! -f $d/kinds.json ]]; then
    MUT_KINDS=$KINDS python3 tools/mutate.py make work/sqlglot $d 50 $((40 + n))
    cp sample/tests.json $d/tests.json
  fi
  echo "$(date '+%F %T') batch $n truth start" >> work/night.log
  nice -n 19 python3 tools/mutate.py truth "$PWD/work/sqlglot" "$PWD/work/.venv/bin/python" "$PWD/$d" "$PWD/sample/tests.json" >> $d/truth.log 2>&1
  got=$(python3 -c "import json;print(len(json.load(open('$d/answers.json'))['commits']))" 2>/dev/null || echo 0)
  if (( got < 50 )); then
    echo "$(date '+%F %T') batch $n stopped with $got of 50 answered (see $d/truth.log)" >> work/night.log
    break
  fi
  touch $d/DONE
  echo "$(date '+%F %T') batch $n done (50 answered)" >> work/night.log
  n=$((n + 1))
done
