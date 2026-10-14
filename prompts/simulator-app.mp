I would like to create a new standalone similator for all of the interface types netflex supports, so cli/tl1/snmp/restconf/netconf. It should be written in golang, and have an api and a web gui.

Features

   * It should keep an inventory of simulators that can be choosen to start/stop/create/delete.
	 * Each simulator directory should have a manifest file that describes what it is.
	 * We currently store our simulator data in /simdata which is an nfs mounted directory, that
	   contains directories for each host so can have seperate simulator files, but also enable
		 copying of simulator files from one host to another. I would like the new simulator UI
		 to be able navigate those directories, so simulators for the new added to this new simulator
		 and.
