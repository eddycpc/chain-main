import base64
import configparser
import datetime
import enum
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List

import bech32
import docker
import durations
import jsonmerge
import multitail2
import tomlkit
import yaml
from dateutil.parser import isoparse
from supervisor import xmlrpc
from supervisor.compat import xmlrpclib

from . import ports
from .utils import build_cli_args_safe, format_doc_string, interact, write_ini

CHAIN = ""  # edit by nix-build
if not CHAIN:
    CHAIN = os.environ.get("CHAIN_MAIND", "chain-maind")
ZEMU_HOST = "127.0.0.1"
ZEMU_BUTTON_PORT = 9997
ZEMU_GRPC_SERVER_PORT = 3002
# dockerfile is integration_test/hardware_wallet/Dockerfile
ZEMU_IMAGE = "cryptocom/builder-zemu:latest"
IMAGE = "docker.pkg.github.com/crypto-com/chain-main/chain-main-pystarport:latest"

COMMON_PROG_OPTIONS = {
    # redirect to supervisord's stdout, easier to collect all logs
    "autostart": "true",
    "autorestart": "true",
    "redirect_stderr": "true",
    "startsecs": "3",
}
SUPERVISOR_CONFIG_FILE = "tasks.ini"


class ModuleAccount(enum.Enum):
    FeeCollector = "fee_collector"
    Mint = "mint"
    Gov = "gov"
    Distribution = "distribution"
    BondedPool = "bonded_tokens_pool"
    NotBondedPool = "not_bonded_tokens_pool"
    IBCTransfer = "transfer"


def home_dir(data_dir, i):
    return data_dir / f"node{i}"


class Ledger:
    def __init__(self):
        self.ledger_name = f"ledger_simulator_{uuid.uuid4().time_mid}"
        self.proxy_name = f"ledger_proxy_{uuid.uuid4().time_mid}"
        self.grpc_name = f"ledger_grpc_server_{uuid.uuid4().time_mid}"
        self.cmds = {
            self.ledger_name: [
                "./speculos/speculos.py",
                "--display=headless",
                f"--button-port={ZEMU_BUTTON_PORT}",
                "./speculos/apps/crypto.elf",
            ],
            self.proxy_name: ["./speculos/tools/ledger-live-http-proxy.py", "-v"],
            self.grpc_name: ["bash", "-c", "RUST_LOG=debug zemu-grpc-server"],
        }
        self.client = docker.from_env()
        self.client.images.pull(ZEMU_IMAGE)
        self.containers = []

    def start(self):
        host_config_ledger = self.client.api.create_host_config(
            auto_remove=True,
            port_bindings={
                ZEMU_BUTTON_PORT: ZEMU_BUTTON_PORT,
                ZEMU_GRPC_SERVER_PORT: ZEMU_GRPC_SERVER_PORT,
            },
        )
        container_ledger = self.client.api.create_container(
            ZEMU_IMAGE,
            self.cmds[self.ledger_name],
            name=self.ledger_name,
            ports=[ZEMU_BUTTON_PORT, ZEMU_GRPC_SERVER_PORT],
            host_config=host_config_ledger,
        )
        self.client.api.start(container_ledger["Id"])
        self.containers.append(container_ledger)
        for name in [self.proxy_name, self.grpc_name]:
            cmd = self.cmds[name]
            try:
                host_config = self.client.api.create_host_config(
                    auto_remove=True, network_mode=f"container:{self.ledger_name}"
                )
                container = self.client.api.create_container(
                    ZEMU_IMAGE,
                    cmd,
                    name=name,
                    host_config=host_config,
                )
                self.client.api.start(container["Id"])
                self.containers.append(container)
                time.sleep(2)
            except Exception as e:
                print(e)

    def stop(self):
        for container in self.containers:
            try:
                self.client.api.remove_container(container["Id"], force=True)
                print("stop docker {}".format(container["Name"]))
            except Exception as e:
                print(e)


class LedgerButton:
    def __init__(self, zemu_address, zemu_button_port):
        self._client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.zemu_address = zemu_address
        self.zemu_button_port = zemu_button_port
        self.connected = False

    @property
    def client(self):
        if not self.connected:
            time.sleep(5)
            self._client.connect((self.zemu_address, self.zemu_button_port))
            self.connected = True
        return self._client

    def press_left(self):
        data = "Ll"
        self.client.send(data.encode())

    def press_right(self):
        data = "Rr"
        self.client.send(data.encode())

    def press_both(self):
        data = "LRlr"
        self.client.send(data.encode())


class ChainCommand:
    def __init__(self, cmd=None):
        self.cmd = cmd or CHAIN

    def __call__(self, cmd, *args, stdin=None, **kwargs):
        "execute chain-maind"
        args = " ".join(build_cli_args_safe(cmd, *args, **kwargs))
        return interact(f"{self.cmd} {args}", input=stdin)


class ClusterCLI:
    "the apis to interact with wallet and blockchain prepared with Cluster"

    def __init__(
        self,
        data,
        chain_id,
        cmd=None,
        zemu_address=ZEMU_HOST,
        zemu_button_port=ZEMU_BUTTON_PORT,
    ):
        self.data_root = data
        self.chain_id = chain_id
        self.data_dir = data / chain_id
        self.config = json.load((self.data_dir / "config.json").open())
        self.raw = ChainCommand(cmd)
        self._supervisorctl = None
        self.leger_button = LedgerButton(zemu_address, zemu_button_port)
        self.output = None
        self.error = None

    @property
    def supervisor(self):
        "http://supervisord.org/api.html"
        # copy from:
        # https://github.com/Supervisor/supervisor/blob/76df237032f7d9fbe80a0adce3829c8b916d5b58/supervisor/options.py#L1718
        if self._supervisorctl is None:
            self._supervisorctl = xmlrpclib.ServerProxy(
                # dumbass ServerProxy won't allow us to pass in a non-HTTP url,
                # so we fake the url we pass into it and
                # always use the transport's
                # 'serverurl' to figure out what to attach to
                "http://127.0.0.1",
                transport=xmlrpc.SupervisorTransport(
                    serverurl=f"unix://{self.data_root}/supervisor.sock"
                ),
            )
        return self._supervisorctl.supervisor

    def reload_supervisor(self):
        subprocess.run(
            [
                sys.executable,
                "-msupervisor.supervisorctl",
                "-c",
                self.data_root / SUPERVISOR_CONFIG_FILE,
                "update",
            ],
            check=True,
        )

    def nodes_len(self):
        "find how many 'node{i}' sub-directories"
        return len(
            [p for p in self.data_dir.iterdir() if re.match(r"^node\d+$", p.name)]
        )

    def create_node(
        self, base_port=None, moniker=None, hostname="localhost", statesync=False
    ):
        """create new node in the data directory,
        process information is written into supervisor config
        start it manually with supervisor commands

        :return: new node index and config
        """
        i = self.nodes_len()

        # default configs
        if base_port is None:
            # use the node0's base_port + i * 10 as default base port for new ndoe
            base_port = self.config["validators"][0]["base_port"] + i * 10
        if moniker is None:
            moniker = f"node{i}"

        # add config
        assert len(self.config["validators"]) == i
        self.config["validators"].append(
            {
                "base_port": base_port,
                "hostname": hostname,
                "moniker": moniker,
            }
        )
        (self.data_dir / "config.json").write_text(json.dumps(self.config))

        # init home directory
        self.init(i)
        home = self.home(i)
        (home / "config/genesis.json").unlink()
        (home / "config/genesis.json").symlink_to("../../genesis.json")
        # use p2p peers from node0's config
        node0 = tomlkit.parse((self.data_dir / "node0/config/config.toml").read_text())

        def custom_edit_tm(doc):
            if statesync:
                info = self.status()["SyncInfo"]
                doc["statesync"].update(
                    {
                        "enable": True,
                        "rpc_servers": ",".join(self.node_rpc(i) for i in range(2)),
                        "trust_height": int(info["earliest_block_height"]),
                        "trust_hash": info["earliest_block_hash"],
                        "temp_dir": str(self.data_dir),
                        "discovery_time": "5s",
                    }
                )

        edit_tm_cfg(
            home / "config/config.toml",
            base_port,
            node0["p2p"]["persistent_peers"],
            custom_edit=custom_edit_tm,
        )
        edit_app_cfg(home / "config/app.toml", base_port)

        # create validator account
        self.create_account("validator", i)

        # add process config into supervisor
        path = self.data_dir / SUPERVISOR_CONFIG_FILE
        ini = configparser.RawConfigParser()
        ini.read_file(path.open())
        chain_id = self.config["chain_id"]
        prgname = f"{chain_id}-node{i}"
        section = f"program:{prgname}"
        ini.add_section(section)
        ini[section].update(
            dict(
                COMMON_PROG_OPTIONS,
                command=f"{self.raw.cmd} start --home %(here)s/node{i}",
                autostart="false",
                stdout_logfile=f"%(here)s/node{i}.log",
            )
        )
        with path.open("w") as fp:
            ini.write(fp)
        self.reload_supervisor()
        return i

    def home(self, i):
        "home directory of i-th node"
        return home_dir(self.data_dir, i)

    def base_port(self, i):
        return self.config["validators"][i]["base_port"]

    def node_rpc(self, i):
        "rpc url of i-th node"
        return "tcp://127.0.0.1:%d" % ports.rpc_port(self.base_port(i))

    # for query
    def ipport_grpc(self, i):
        "grpc url of i-th node"
        return "127.0.0.1:%d" % ports.grpc_port(self.base_port(i))

    # tx broadcast only
    def ipport_grpc_tx(self, i):
        "grpc url of i-th node"
        return "127.0.0.1:%d" % ports.grpc_port_tx_only(self.base_port(i))

    def node_id(self, i):
        "get i-th node's tendermint node id"
        output = self.raw("tendermint", "show-node-id", home=self.home(i))
        return output.decode().strip()

    def create_account(self, name, i=0, mnemonic=None):
        "create new keypair in i-th node's keyring"
        if mnemonic is None:
            output = self.raw(
                "keys",
                "add",
                name,
                home=self.home(i),
                output="json",
                keyring_backend="test",
            )
        else:
            output = self.raw(
                "keys",
                "add",
                name,
                "--recover",
                home=self.home(i),
                output="json",
                keyring_backend="test",
                stdin=mnemonic.encode() + b"\n",
            )
        return json.loads(output)

    def create_account_ledger(self, name, i=0):
        "create new ledger keypair"

        def send_request():
            try:
                self.output = self.raw(
                    "keys",
                    "add",
                    name,
                    "--ledger",
                    home=self.home(i),
                    output="json",
                    keyring_backend="test",
                )
            except Exception as e:
                self.error = e

        t = threading.Thread(target=send_request)
        t.start()
        time.sleep(3)
        for _ in range(0, 3):
            self.leger_button.press_right()
            time.sleep(0.2)
        self.leger_button.press_both()
        t.join()
        if self.error:
            raise self.error
        return json.loads(self.output)

    def init(self, i):
        "the i-th node's config is already added"
        return self.raw(
            "init",
            self.config["validators"][i]["moniker"],
            chain_id=self.chain_id,
            home=self.home(i),
        )

    def validate_genesis(self, i=0):
        return self.raw("validate-genesis", home=self.home(i))

    def add_genesis_account(self, addr, coins, i=0, **kwargs):
        return self.raw(
            "add-genesis-account",
            addr,
            coins,
            home=self.home(i),
            output="json",
            **kwargs,
        )

    def gentx(self, name, coins, i, min_self_delegation=1):
        return self.raw(
            "gentx",
            name,
            coins,
            amount=coins,
            min_self_delegation=str(min_self_delegation),
            home=self.home(i),
            chain_id=self.chain_id,
            keyring_backend="test",
        )

    def collect_gentxs(self, gentx_dir, i=0):
        return self.raw("collect-gentxs", gentx_dir, home=self.home(i))

    def status(self, i=0):
        return json.loads(self.raw("status", node=self.node_rpc(i)))

    def block_height(self, i=0):
        return int(self.status(i)["SyncInfo"]["latest_block_height"])

    def block_time(self, i=0):
        return isoparse(self.status(i)["SyncInfo"]["latest_block_time"])

    def balance(self, addr, i=0):
        coin = json.loads(
            self.raw(
                "query", "bank", "balances", addr, output="json", node=self.node_rpc(i)
            )
        )["balances"]
        if len(coin) == 0:
            return 0
        coin = coin[0]
        assert coin["denom"] == "basecro"
        return int(coin["amount"])

    def distribution_commision(self, addr, i=0):
        coin = json.loads(
            self.raw(
                "query",
                "distribution",
                "commission",
                addr,
                output="json",
                node=self.node_rpc(i),
            )
        )["commission"][0]
        return float(coin["amount"])

    def distribution_community(self, i=0):
        coin = json.loads(
            self.raw(
                "query",
                "distribution",
                "community-pool",
                output="json",
                node=self.node_rpc(i),
            )
        )["pool"][0]
        return float(coin["amount"])

    def distribution_reward(self, delegator_addr, i=0):
        coin = json.loads(
            self.raw(
                "query",
                "distribution",
                "rewards",
                delegator_addr,
                output="json",
                node=self.node_rpc(i),
            )
        )["total"][0]
        return float(coin["amount"])

    def address(self, name, i=0, bech="acc"):
        output = self.raw(
            "keys",
            "show",
            name,
            "-a",
            home=self.home(i),
            keyring_backend="test",
            bech=bech,
        )
        return output.strip().decode()

    @format_doc_string(
        options=",".join(v.value for v in ModuleAccount.__members__.values())
    )
    def module_address(self, name):
        """
        get address of module accounts

        :param name: name of module account, values: {options}
        """
        data = hashlib.sha256(ModuleAccount(name).value.encode()).digest()[:20]
        return bech32.bech32_encode("cro", bech32.convertbits(data, 8, 5))

    def account(self, addr, i=0):
        return json.loads(
            self.raw(
                "query", "auth", "account", addr, output="json", node=self.node_rpc(i)
            )
        )

    def supply(self, supply_type):
        return json.loads(
            self.raw(
                "query", "supply", supply_type, output="json", node=self.node_rpc(0)
            )
        )

    def validator(self, addr, i=0):
        return json.loads(
            self.raw(
                "query",
                "staking",
                "validator",
                addr,
                output="json",
                node=self.node_rpc(i),
            )
        )

    def validators(self, i=0):
        return json.loads(
            self.raw(
                "query", "staking", "validators", output="json", node=self.node_rpc(i)
            )
        )["validators"]

    def staking_pool(self, bonded=True):
        return int(
            json.loads(
                self.raw(
                    "query", "staking", "pool", output="json", node=self.node_rpc(0)
                )
            )["bonded_tokens" if bonded else "not_bonded_tokens"]
        )

    def transfer(self, from_, to, coins, i=0, generate_only=False, fees=None):
        return json.loads(
            self.raw(
                "tx",
                "bank",
                "send",
                from_,
                to,
                coins,
                "-y",
                "--generate-only" if generate_only else None,
                home=self.home(i),
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
                fees=fees,
            )
        )

    def transfer_from_ledger(
        self, from_, to, coins, i=0, generate_only=False, fees=None
    ):
        def send_request():
            try:
                self.output = self.raw(
                    "tx",
                    "bank",
                    "send",
                    from_,
                    to,
                    coins,
                    "-y",
                    "--generate-only" if generate_only else "",
                    "--ledger",
                    home=self.home(i),
                    keyring_backend="test",
                    chain_id=self.chain_id,
                    node=self.node_rpc(0),
                    fees=fees,
                    sign_mode="amino-json",
                )
            except Exception as e:
                self.error = e

        t = threading.Thread(target=send_request)
        t.start()
        time.sleep(3)
        for _ in range(0, 11):
            self.leger_button.press_right()
            time.sleep(0.4)
        self.leger_button.press_both()
        t.join()
        if self.error:
            raise self.error
        return json.loads(self.output)

    def get_delegated_amount(self, which_addr, i=0):
        return json.loads(
            self.raw(
                "query",
                "staking",
                "delegations",
                which_addr,
                home=self.home(i),
                chain_id=self.chain_id,
                node=self.node_rpc(0),
                output="json",
            )
        )

    def delegate_amount(self, to_addr, amount, from_addr, i=0):
        return json.loads(
            self.raw(
                "tx",
                "staking",
                "delegate",
                to_addr,
                amount,
                "-y",
                home=self.home(i),
                from_=from_addr,
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    # to_addr: croclcl1...  , from_addr: cro1...
    def unbond_amount(self, to_addr, amount, from_addr, i=0):
        return json.loads(
            self.raw(
                "tx",
                "staking",
                "unbond",
                to_addr,
                amount,
                "-y",
                home=self.home(i),
                from_=from_addr,
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    # to_validator_addr: crocncl1...  ,  from_from_validator_addraddr: crocl1...
    def redelegate_amount(
        self, to_validator_addr, from_validator_addr, amount, from_addr, i=0
    ):
        return json.loads(
            self.raw(
                "tx",
                "staking",
                "redelegate",
                from_validator_addr,
                to_validator_addr,
                amount,
                "-y",
                home=self.home(i),
                from_=from_addr,
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    def make_multisig(self, name, signer1, signer2, i=0):
        self.raw(
            "keys",
            "add",
            name,
            multisig=f"{signer1},{signer2}",
            multisig_threshold="2",
            home=self.home(i),
            keyring_backend="test",
            output="json",
        )

    def sign_multisig_tx(self, tx_file, multi_addr, signer_name, i=0):
        return json.loads(
            self.raw(
                "tx",
                "sign",
                tx_file,
                from_=signer_name,
                multisig=multi_addr,
                home=self.home(i),
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    def encode_signed_tx(self, signed_tx):
        return self.raw(
            "tx",
            "encode",
            signed_tx,
        )

    def sign_single_tx(self, tx_file, signer_name, i=0):
        return json.loads(
            self.raw(
                "tx",
                "sign",
                tx_file,
                from_=signer_name,
                home=self.home(i),
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    def combine_multisig_tx(self, tx_file, multi_name, signer1_file, signer2_file, i=0):
        return json.loads(
            self.raw(
                "tx",
                "multisign",
                tx_file,
                multi_name,
                signer1_file,
                signer2_file,
                home=self.home(i),
                keyring_backend="test",
                chain_id=self.chain_id,
                node=self.node_rpc(0),
            )
        )

    def broadcast_tx(self, tx_file, i=0):
        return json.loads(self.raw("tx", "broadcast", tx_file, node=self.node_rpc(i)))

    def unjail(self, addr, i=0):
        return json.loads(
            self.raw(
                "tx",
                "slashing",
                "unjail",
                "-y",
                from_=addr,
                home=self.home(i),
                node=self.node_rpc(i),
                keyring_backend="test",
                chain_id=self.chain_id,
            )
        )

    def create_validator(
        self,
        amount,
        i,
        moniker=None,
        commission_max_change_rate="0.01",
        commission_rate="0.1",
        commission_max_rate="0.2",
        min_self_delegation="1",
        identity="",
        website="",
        security_contact="",
        details="",
    ):
        """MsgCreateValidator
        create the node with create_node before call this"""
        pubkey = (
            self.raw("tendermint", "show-validator", home=self.home(i)).strip().decode()
        )
        return json.loads(
            self.raw(
                "tx",
                "staking",
                "create-validator",
                "-y",
                from_=self.address("validator", i),
                amount=amount,
                pubkey=pubkey,
                min_self_delegation=min_self_delegation,
                # commision
                commission_rate=commission_rate,
                commission_max_rate=commission_max_rate,
                commission_max_change_rate=commission_max_change_rate,
                # description
                moniker=moniker or self.config["validators"][i]["moniker"],
                identity=identity,
                website=website,
                security_contact=security_contact,
                details=details,
                # basic
                home=self.home(i),
                node=self.node_rpc(0),
                keyring_backend="test",
                chain_id=self.chain_id,
            )
        )

    def edit_validator(
        self,
        i,
        commission_rate=None,
        moniker=None,
        identity=None,
        website=None,
        security_contact=None,
        details=None,
    ):
        """MsgEditValidator"""
        options = dict(
            commission_rate=commission_rate,
            # description
            moniker=moniker,
            identity=identity,
            website=website,
            security_contact=security_contact,
            details=details,
        )
        return json.loads(
            self.raw(
                "tx",
                "staking",
                "edit-validator",
                "-y",
                from_=self.address("validator", i),
                home=self.home(i),
                node=self.node_rpc(0),
                keyring_backend="test",
                chain_id=self.chain_id,
                **{k: v for k, v in options.items() if v is not None},
            )
        )

    def gov_propose(self, proposor, kind, proposal, i=0):
        if kind == "software-upgrade":
            return json.loads(
                self.raw(
                    "tx",
                    "gov",
                    "submit-proposal",
                    kind,
                    proposal["name"],
                    "-y",
                    from_=proposor,
                    # content
                    title=proposal.get("title"),
                    description=proposal.get("description"),
                    upgrade_height=proposal.get("upgrade-height"),
                    upgrade_time=proposal.get("upgrade-time"),
                    upgrade_info=proposal.get("upgrade-info"),
                    deposit=proposal.get("deposit"),
                    # basic
                    home=self.home(i),
                    node=self.node_rpc(0),
                    keyring_backend="test",
                    chain_id=self.chain_id,
                )
            )
        elif kind == "cancel-software-upgrade":
            return json.loads(
                self.raw(
                    "tx",
                    "gov",
                    "submit-proposal",
                    kind,
                    "-y",
                    from_=proposor,
                    # content
                    title=proposal.get("title"),
                    description=proposal.get("description"),
                    deposit=proposal.get("deposit"),
                    # basic
                    home=self.home(i),
                    node=self.node_rpc(0),
                    keyring_backend="test",
                    chain_id=self.chain_id,
                )
            )
        else:
            with tempfile.NamedTemporaryFile("w") as fp:
                json.dump(proposal, fp)
                fp.flush()
                return json.loads(
                    self.raw(
                        "tx",
                        "gov",
                        "submit-proposal",
                        kind,
                        fp.name,
                        "-y",
                        from_=proposor,
                        # basic
                        home=self.home(i),
                        node=self.node_rpc(0),
                        keyring_backend="test",
                        chain_id=self.chain_id,
                    )
                )

    def gov_vote(self, voter, proposal_id, option, i=0):
        return json.loads(
            self.raw(
                "tx",
                "gov",
                "vote",
                proposal_id,
                option,
                "-y",
                from_=voter,
                home=self.home(i),
                node=self.node_rpc(0),
                keyring_backend="test",
                chain_id=self.chain_id,
            )
        )

    def gov_deposit(self, depositor, proposal_id, amount, i=0):
        return json.loads(
            self.raw(
                "tx",
                "gov",
                "deposit",
                proposal_id,
                amount,
                "-y",
                from_=depositor,
                home=self.home(i),
                node=self.node_rpc(0),
                keyring_backend="test",
                chain_id=self.chain_id,
            )
        )

    def query_proposals(self, depositor=None, limit=None, status=None, voter=None):
        return json.loads(
            self.raw(
                "query",
                "gov",
                "proposals",
                depositor=depositor,
                count_total=limit,
                status=status,
                voter=voter,
                output="json",
                node=self.node_rpc(0),
            )
        )

    def query_proposal(self, proposal_id):
        return json.loads(
            self.raw(
                "query",
                "gov",
                "proposal",
                proposal_id,
                output="json",
                node=self.node_rpc(0),
            )
        )

    def query_tally(self, proposal_id):
        return json.loads(
            self.raw(
                "query",
                "gov",
                "tally",
                proposal_id,
                output="json",
                node=self.node_rpc(0),
            )
        )

    def ibc_transfer(
        self,
        from_,
        to,
        amount,
        channel,  # src channel
        target_version,  # chain version number of target chain
        i=0,
    ):
        return json.loads(
            self.raw(
                "tx",
                "ibc-transfer",
                "transfer",
                "transfer",  # src port
                channel,
                to,
                amount,
                "-y",
                # FIXME https://github.com/cosmos/cosmos-sdk/issues/8059
                "--absolute-timeouts",
                from_=from_,
                home=self.home(i),
                node=self.node_rpc(i),
                keyring_backend="test",
                chain_id=self.chain_id,
                packet_timeout_height=f"{target_version}-10000000000",
                packet_timeout_timestamp=0,
            )
        )


def start_cluster(data_dir):
    cmd = [
        sys.executable,
        "-msupervisor.supervisord",
        "-c",
        data_dir / SUPERVISOR_CONFIG_FILE,
    ]
    return subprocess.Popen(cmd, env=dict(os.environ, PYTHONPATH=":".join(sys.path)))


class TailLogsThread(threading.Thread):
    def __init__(self, base_dir, pats: List[str]):
        self.base_dir = base_dir
        self.tailer = multitail2.MultiTail([str(base_dir / pat) for pat in pats])
        self._stop_event = threading.Event()
        super().__init__()

    def run(self):
        while not self.stopped:
            for (path, _), s in self.tailer.poll():
                print(Path(path).relative_to(self.base_dir), s)

            # TODO Replace this with FAM/inotify for watching filesystem events.
            time.sleep(0.5)

    def stop(self):
        self._stop_event.set()

    @property
    def stopped(self):
        return self._stop_event.is_set()


def start_tail_logs_thread(data_dir):
    t = TailLogsThread(data_dir, ["*/node*.log", "relayer-*.log"])
    t.start()
    return t


def process_config(config, base_port):
    """
    fill default values in config
    """
    for i, val in enumerate(config["validators"]):
        if "moniker" not in val:
            val["moniker"] = f"node{i}"
        if "base_port" not in val:
            val["base_port"] = base_port + i * 10
        if "hostname" not in val:
            val["hostname"] = "localhost"


def init_devnet(
    data_dir,
    config,
    base_port,
    image=IMAGE,
    cmd=None,
    gen_compose_file=False,
):
    """
    init data directory
    """

    def create_account(cli, account, use_ledger=False):
        if use_ledger:
            acct = cli.create_account_ledger(account["name"])
        else:
            acct = cli.create_account(account["name"])
        vesting = account.get("vesting")
        if not vesting:
            cli.add_genesis_account(acct["address"], account["coins"])
        else:
            genesis_time = isoparse(genesis["genesis_time"])
            end_time = genesis_time + datetime.timedelta(
                seconds=durations.Duration(vesting).to_seconds()
            )
            vend = int(end_time.timestamp())
            cli.add_genesis_account(
                acct["address"],
                account["coins"],
                vesting_amount=account["coins"],
                vesting_end_time=vend,
            )
        return acct

    process_config(config, base_port)

    (data_dir / "config.json").write_text(json.dumps(config))

    cmd = cmd or CHAIN

    # init home directories
    for i, val in enumerate(config["validators"]):
        ChainCommand(cmd)(
            "init",
            val["moniker"],
            chain_id=config["chain_id"],
            home=home_dir(data_dir, i),
        )
        if "consensus_key" in val:
            # restore consensus private key
            with (home_dir(data_dir, i) / "config/priv_validator_key.json").open(
                "w"
            ) as fp:
                json.dump(
                    {
                        "address": hashlib.sha256(
                            base64.b64decode(val["consensus_key"]["pub"])
                        )
                        .hexdigest()[:40]
                        .upper(),
                        "pub_key": {
                            "type": "tendermint/PubKeyEd25519",
                            "value": val["consensus_key"]["pub"],
                        },
                        "priv_key": {
                            "type": "tendermint/PrivKeyEd25519",
                            "value": val["consensus_key"]["priv"],
                        },
                    },
                    fp,
                )
    if "genesis_file" in config:
        genesis_bytes = open(
            config["genesis_file"] % {"here": Path(config["path"]).parent}, "rb"
        ).read()
    else:
        genesis_bytes = (data_dir / "node0/config/genesis.json").read_bytes()
    (data_dir / "genesis.json").write_bytes(genesis_bytes)
    (data_dir / "gentx").mkdir()
    for i in range(len(config["validators"])):
        try:
            (data_dir / f"node{i}/config/genesis.json").unlink()
        except OSError:
            pass
        (data_dir / f"node{i}/config/genesis.json").symlink_to("../../genesis.json")
        (data_dir / f"node{i}/config/gentx").symlink_to("../../gentx")

    # now we can create ClusterCLI
    cli = ClusterCLI(data_dir.parent, config["chain_id"], cmd)

    # patch the genesis file
    genesis = jsonmerge.merge(
        json.load(open(data_dir / "genesis.json")),
        config.get("genesis", {}),
    )
    (data_dir / "genesis.json").write_text(json.dumps(genesis))
    cli.validate_genesis()

    # create accounts
    accounts = []
    for i, node in enumerate(config["validators"]):
        mnemonic = node.get("mnemonic")
        account = cli.create_account("validator", i, mnemonic=mnemonic)
        accounts.append(account)
        if "coins" in node:
            cli.add_genesis_account(account["address"], node["coins"], i)
        if "staked" in node:
            cli.gentx(
                "validator",
                node["staked"],
                i,
                min_self_delegation=node.get("min_self_delegation", 1),
            )

    # create accounts
    for account in config.get("accounts", []):
        account = create_account(cli, account)
        accounts.append(account)

    account_hw = config.get("hw_account")
    if account_hw:
        account = create_account(cli, account_hw, True)
        accounts.append(account)

    # output accounts
    (data_dir / "accounts.json").write_text(json.dumps(accounts))

    # collect-gentxs if directory not empty
    if next((data_dir / "gentx").iterdir(), None) is not None:
        cli.collect_gentxs(data_dir / "gentx", 0)

    # realise the symbolic links, so the node directories can be used independently
    genesis_bytes = (data_dir / "genesis.json").read_bytes()
    for i in range(len(config["validators"])):
        (data_dir / f"node{i}/config/gentx").unlink()
        tmp = data_dir / f"node{i}/config/genesis.json"
        tmp.unlink()
        tmp.write_bytes(genesis_bytes)

    # write tendermint config
    peers = config.get("peers") or ",".join(
        [
            "tcp://%s@%s:%d"
            % (cli.node_id(i), val["hostname"], ports.p2p_port(val["base_port"]))
            for i, val in enumerate(config["validators"])
        ]
    )
    for i, val in enumerate(config["validators"]):
        edit_tm_cfg(data_dir / f"node{i}/config/config.toml", val["base_port"], peers)
        edit_app_cfg(data_dir / f"node{i}/config/app.toml", val["base_port"])

    # write supervisord config file
    with (data_dir / SUPERVISOR_CONFIG_FILE).open("w") as fp:
        write_ini(fp, supervisord_ini(cmd, config["validators"], config["chain_id"]))

    if gen_compose_file:
        yaml.dump(
            docker_compose_yml(cmd, config["validators"], data_dir, image),
            (data_dir / "docker-compose.yml").open("w"),
        )


def relayer_chain_config(data_dir, chain_id):
    cfg = json.load((data_dir / chain_id / "config.json").open())
    rpc_port = ports.rpc_port(cfg["validators"][0]["base_port"])
    return {
        "key": "relayer",
        "chain-id": chain_id,
        # rpc address of first node
        "rpc-addr": f"http://localhost:{rpc_port}",
        "account-prefix": "cro",
        "gas-adjustment": 1.5,
        "gas-prices": "0.0basecro",
        "trusting-period": "336h",
        "debug": True,
    }


def init_cluster(
    data_dir, config_path, base_port, image=IMAGE, cmd=None, gen_compose_file=False
):
    config = yaml.safe_load(open(config_path))

    # override relayer config in config.yaml
    rly_section = config.pop("relayer", {})
    for chain_id, cfg in config.items():
        cfg["path"] = str(config_path)
        cfg["chain_id"] = chain_id

    chains = list(config.values())
    for chain in chains:
        (data_dir / chain["chain_id"]).mkdir()
        init_devnet(
            data_dir / chain["chain_id"], chain, base_port, image, cmd, gen_compose_file
        )
    with (data_dir / SUPERVISOR_CONFIG_FILE).open("w") as fp:
        write_ini(
            fp,
            supervisord_ini_group(
                config.keys(), list(rly_section.get("paths", {}).keys())
            ),
        )
    if len(chains) > 1:
        # write relayer config
        rly_home = data_dir / "relayer"
        rly_home.mkdir()
        rly_cfg = rly_home / "config/config.yaml"
        rly_cfg.parent.mkdir()
        rly_section["chains"] = [
            relayer_chain_config(data_dir, chain_id) for chain_id in config
        ]
        with rly_cfg.open("w") as fp:
            yaml.dump(rly_section, fp)

        # restore the relayer account in relayer
        for chain in chains:
            mnemonic = find_account(data_dir, chain["chain_id"], "relayer")["mnemonic"]
            subprocess.run(
                [
                    "relayer",
                    "--home",
                    rly_home,
                    "keys",
                    "restore",
                    chain["chain_id"],
                    "relayer",
                    mnemonic,
                    "--coin-type",
                    "394",  # mainnet cro
                ],
                check=True,
            )


def find_account(data_dir, chain_id, name):
    accounts = json.load((data_dir / chain_id / "accounts.json").open())
    return next(acct for acct in accounts if acct["name"] == name)


def supervisord_ini(cmd, validators, chain_id):
    ini = {}
    for i, node in enumerate(validators):
        ini[f"program:{chain_id}-node{i}"] = dict(
            COMMON_PROG_OPTIONS,
            command=f"{cmd} start --home %(here)s/node{i}",
            stdout_logfile=f"%(here)s/node{i}.log",
        )
    return ini


def supervisord_ini_group(chain_ids, paths):
    cfg = {
        "include": {
            "files": " ".join(
                f"%(here)s/{chain_id}/tasks.ini" for chain_id in chain_ids
            )
        },
        "supervisord": {
            "pidfile": "%(here)s/supervisord.pid",
            "nodaemon": "true",
            "logfile": "/dev/null",
            "logfile_maxbytes": "0",
        },
        "rpcinterface:supervisor": {
            "supervisor.rpcinterface_factory": "supervisor.rpcinterface:"
            "make_main_rpcinterface",
        },
        "unix_http_server": {"file": "%(here)s/supervisor.sock"},
        "supervisorctl": {"serverurl": "unix://%(here)s/supervisor.sock"},
    }
    for path in paths:
        cfg[f"program:relayer-{path}"] = dict(
            COMMON_PROG_OPTIONS,
            command=f"relayer --home %(here)s/relayer tx link-then-start {path}",
            stdout_logfile=f"%(here)s/relayer-{path}.log",
        )
    return cfg


def docker_compose_yml(cmd, validators, data_dir, image):
    return {
        "version": "3",
        "services": {
            f"node{i}": {
                "image": image,
                "command": "chaind start",
                "volumes": [f"{data_dir.absolute() / f'node{i}'}:/.chain-maind:Z"],
            }
            for i, val in enumerate(validators)
        },
    }


def edit_tm_cfg(path, base_port, peers, *, custom_edit=None):
    doc = tomlkit.parse(open(path).read())
    # tendermint is start in process, not needed
    # doc['proxy_app'] = 'tcp://127.0.0.1:%d' % abci_port(base_port)
    doc["rpc"]["laddr"] = "tcp://0.0.0.0:%d" % ports.rpc_port(base_port)
    doc["rpc"]["pprof_laddr"] = "localhost:%d" % ports.pprof_port(base_port)
    doc["rpc"]["grpc_laddr"] = "tcp://0.0.0.0:%d" % ports.grpc_port_tx_only(base_port)
    doc["p2p"]["laddr"] = "tcp://0.0.0.0:%d" % ports.p2p_port(base_port)
    doc["p2p"]["persistent_peers"] = peers
    doc["p2p"]["addr_book_strict"] = False
    doc["p2p"]["allow_duplicate_ip"] = True
    doc["consensus"]["timeout_commit"] = "1s"
    doc["rpc"]["timeout_broadcast_tx_commit"] = "30s"
    if custom_edit is not None:
        custom_edit(doc)
    open(path, "w").write(tomlkit.dumps(doc))


def edit_app_cfg(path, base_port):
    doc = tomlkit.parse(open(path).read())
    # enable api server
    doc["api"]["enable"] = True
    doc["api"]["swagger"] = True
    doc["api"]["enabled-unsafe-cors"] = True
    doc["api"]["address"] = "tcp://0.0.0.0:%d" % ports.api_port(base_port)
    doc["grpc"]["address"] = "0.0.0.0:%d" % ports.grpc_port(base_port)
    # take snapshot for statesync
    doc["pruning"] = "nothing"
    doc["state-sync"]["snapshot-interval"] = 5
    doc["state-sync"]["snapshot-keep-recent"] = 10
    open(path, "w").write(tomlkit.dumps(doc))


if __name__ == "__main__":
    interact("rm -r data; mkdir data", ignore_error=True)
    data_dir = Path("data")
    init_cluster(data_dir, "config.yaml", 26650)
    supervisord = start_cluster(data_dir)
    t = start_tail_logs_thread(data_dir)
    supervisord.wait()
    t.stop()
    t.join()
