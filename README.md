### FAUCET gNMI Configuration Agent

[![Build/Test Status][1]][2]

This agent exposes a simple gNMI service to configure [FAUCET][3].
Requires FAUCET version 1.9.3 or later.

For now, it simply allows you to get or replace the entire
FAUCET configuration file (e.g. `faucet.yaml`) via gNMI path `/`.

### Starting up the agent

    ./faucetagent.py --cert agent.crt --key agent.key \
       --configfile /etc/faucet.yaml  >& faucetagent.log &

### Talking to the agent using [gnxi][4]

    # TLS authentication (client auth is ignored by agent atm)
    AUTH="-ca ca.crt -cert client.crt -key client.key -target_name localhost"

    # Extract string_val from gnmi_get output
    string_val() { grep string_val: | awk -F 'string_val: "' '{printf $2;}'  |
                   sed -e 's/"$//' | xargs -0 printf; }

    # Fetch information about configuration schema
    gnmi_capabilities $AUTH

    # Fetch current configuration
    gnmi_get $AUTH -xpath=/ | string_val

    # Send a configuration file to FAUCET
    gnmi_set $AUTH -replace=/:"$(<faucet.yaml)"

### Simple end-to-end test using [mininet][5]

    ./dependencies.sh
    ./test-dependencies.sh
    make
    sudo make test

[1]: https://github.com/faucetsdn/faucetagent/workflows/faucetagent%20tests/badge.svg
[2]: https://github.com/faucetsdn/faucetagent/actions
[3]: https://github.com/faucetsdn/faucet
[4]: https://github.com/google/gnxi
[5]: https://github.com/mininet/mininet
