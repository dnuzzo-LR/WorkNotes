
jgomillion-LR


per Charter, the Infinera node user lockout issue is triggered when multiple sessions are attempted to be started on multiple RT's behind a GNE. When that happens it locks up the Infinera CPU and they have to reseat the card to restore it. My ask is to slow the login process down on RT's after the GNE login is successful. If there was a 1 or 2 second delay per RT node to log them in 1 at a time that would prevent the problem. Once the login is good DNO and other processes can run as normal.

the other problem is that we have 2 BEP's talking to the same RT nodes. IDK if the protect BEP can wait until the working BEP has brought all the links up first. Or maybe if the BEP is in standby it has a 5 minute wait timer before it begins bringing the links online.



 when two Infinera DTN/DTN-X RT's try to login at the same time through a GNE, it causes failures, that end up locking out the user account used to login. read the ./lan.c and lanalive.c and tell be if the RtLoginDelay variable can be used to slove this issue? If not propose a change


to add a tunable delay when logging into the RT's


  ## Code Search

  Prefer `ast-grep` over grep/sed/awk for code structure queries.

  - Finding function defs, calls, class decls, type usage, control-flow patterns → ast-grep
  - Refactors (rename, rewrite signature, restructure expressions) → ast-grep `--rewrite`
  - Multi-line / syntax-aware matches → ast-grep
  - Fall back to grep ONLY for: log strings, comments, config values, plain text, filenames

52.141 Patch  co inc52.141

d609c573272ed03026cdf0fa983f17a661d9681b  sdisrc.mk xlansim missing -lsdi
a28a8e0d6f28530161eb2ab436e11766f5fb08e7  the big change
be6ff40e042f05b840ff87bd35012dd296dad6f4  3408 friendly

RtLoginDelay
GneRtLoginGap

a28a8e0d6q



62325b21945e362e71af2f111753bd37573df8bb

rebuild products: lan, libsdi.so, liblan.so
restart processes: NO
restart INC application? (YES/NO): NO

Description
add new rt delay timer to lan

closes #3684




