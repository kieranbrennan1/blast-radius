#!/bin/sh
# Run the built image once on the sample under the scorer's limits and print the CPU seconds it used.
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 \
  --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 \
  --env HOME=/tmp --volume "$PWD/sample/commits:/data/commits:ro" --volume "$PWD/sample/tests.json:/data/tests.json:ro" \
  blast-radius-entry python -c "
import resource, subprocess, sys, time
t = time.time()
subprocess.run(['python', '-S', '/app/selector.py'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
r = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f'selector CPU {r.ru_utime + r.ru_stime:.2f} s, wall {time.time() - t:.2f} s')
"
