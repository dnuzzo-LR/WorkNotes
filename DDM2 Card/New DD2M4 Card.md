#### From Jake Barrow

if you need any info on the dd2m4s let me know. unfortunately we don't have anywhere we can put up a completely blank one but the one that I provisioned for the wvlsvc stuff can be taken down as you need and I can reconfigure it after that. it's nothing that like the sfm6 card on that NE type doesn't do


the new card is 2 line ports, 4 client ports vs 1 line port, 6 client ports, etc. but what each thing does and all the rates and stuff are the same as the sfm6

![[Pasted image 20250212095954.png]]


|     |        |                               | SNMP attribute       | ObjectID |     |                                                                    |
| --- | ------ | ----------------------------- | -------------------- | -------- | --- | ------------------------------------------------------------------ |
| L1  |        |                               | tnAsonTopoClearAlarm | 17039616 |     |                                                                    |
|     |        |                               | tnNwPortLinkSpan     | 17039616 |     |                                                                    |
|     |        |                               | tnAccessPortDescr    | 17039616 |     |                                                                    |
|     |        | Expected Network Output Power |                      |          |     |                                                                    |
| C1  | OTU    | Pluggable Module Type         |                      |          |     | Auto/Q28LR4d/Q28SR4d/User                                          |
| C2  | 100GBE | Pluggable Module Type         |                      |          |     | Q28SR4d/Q28SR4e/Q28CWDM4/Q28LR1e/Q28LR4e/Q28LR4d/Q28ER4d/Auto/User |
|     | 400GBE | Pluggable Module Type         |                      |          |     | Q56DD-400G-FR4/Q56DD-400G-LR4/Q56DD-400G-LR8/Auto/User             |


      PMT Auto/Q28LR4d/Q28SR4d/User
       SSF Delay Timer
       No Fec Mode

      Client Port OTU
	        FEC Mode  [RSFEC/]