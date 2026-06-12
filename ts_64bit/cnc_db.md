

##### userproc/src/cnc_db.c

```
+ /home/dan/wbin/sget srf=cnc_db.c
--- /home/dan/inc/51.1/cnc/userproc/src/orig.cnc_db.c 2025-02-28 11:28:23.066440814 -0500
+++ /home/dan/inc/51.1/cnc/userproc/src/cnc_db.c  2025-02-28 11:26:40.219644635 -0500
@@ -67,13 +67,18 @@
              if (Ds_port[d]->frame_num != VDACS24
                && Ds_port[d]->frame_num != VDACS28
                && Ds_port[d]->frame_num != (get_info & GET_DACSID))
                continue;
            }
+#ifndef __LP64__
            if ((get_info & GET_EST)
              && !(Ds_port[d]->channel[t].info & ESTABLISHED))
              continue;
+#else
+           if ((get_info & GET_EST))
+             continue;
+#endif
            else if ((get_info & GET_CONN)
              && Ds_port[d]->channel[t].xcc == DNULL)
              continue;
            break;

```
