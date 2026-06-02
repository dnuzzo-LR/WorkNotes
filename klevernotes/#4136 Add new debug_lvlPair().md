#4136 Add new debug_lvlPair() so processes can configure trace/debug for neid/dtype only
#4136


start  holmvm66 15000 MultiBox Active
841f6a561c38d3826c2db91bc6c86ea94db6f64e

4156


dtn 

d609c573272ed03026cdf0fa983f17a661d9681b xlansim -lsdi sdisrc.mk
a28a8e0d6f28530161eb2ab436e11766f5fb08e7


    Files:
      cnc/sdi/src/comm_tunable.c    - parse GNERTLOGINGAP, clamp
      cnc/sdi/src/comm_tunable_s.c  - parse GNERTLOGINGAP, clamp
      cnc/sdi/src/lanalive.c        - pacing logic
      include/comm_tunable.h        - GNE_RT_LOGIN_GAP_MAX

2747
