import argparse
import asyncio
import ipaddress

from quarkchain.config import DEFAULT_ENV
from quarkchain.cluster.rpc import ConnectToSlavesResponse, ClusterOp, CLUSTER_OP_SERIALIZER_MAP, Ping, Pong
from quarkchain.protocol import Connection
from quarkchain.db import PersistentDb
from quarkchain.utils import is_shard_in_mask, set_logging_level, Logger


class MasterConnection(Connection):

    def __init__(self, env, reader, writer, slaveServer):
        super().__init__(env, reader, writer, CLUSTER_OP_SERIALIZER_MAP, MASTER_OP_NONRPC_MAP, MASTER_OP_RPC_MAP)
        self.loop = asyncio.get_event_loop()
        self.env = env
        self.slaveServer = slaveServer

        asyncio.ensure_future(self.activeAndLoopForever())

    def __getShardSize(self):
        return self.env.config.SHARD_SIZE

    async def handlePing(self, ping):
        return Pong(self.slaveServer.id, self.slaveServer.shardMaskList)

    async def handleConnectToSlavesRequest(self, connectToSlavesRequest):
        ''' Master sends in the slave list. Let's connect to them.
        Skip self and slaves already connected.
        '''
        resultList = []
        for slaveInfo in connectToSlavesRequest.slaveInfoList:
            if slaveInfo.id == self.slaveServer.id or slaveInfo.id in self.slaveServer.slaveIds:
                resultList.append(bytes())
                continue

            ip = str(ipaddress.ip_address(slaveInfo.ip))
            port = slaveInfo.port
            try:
                reader, writer = await asyncio.open_connection(ip, port, loop=self.loop)
            except Exception as e:
                errMsg = "Failed to connect {}:{} with exception {}".format(ip, port, e)
                Logger.info(errMsg)
                resultList.append(bytes(errMsg, "ascii"))
                continue

            slave = SlaveConnection(self.env, reader, writer, self.slaveServer, slaveInfo.id, slaveInfo.shardMaskList)
            await slave.waitUntilActive()
            # Tell the remote slave who I am
            id, shardMaskList = await slave.sendPing()
            # Verify that remote slave indeed has the id and shard mask list advertised by the master
            if id != slave.id:
                resultList.append(bytes("id does not match. expect {} got {}".format(slave.id, id), "ascii"))
                continue
            if shardMaskList != slave.shardMaskList:
                resultList.append(bytes("shard mask list does not match. expect {} got {}".format(
                    slave.shardMaskList, shardMaskList), "ascii"))
                continue

            self.slaveServer.addSlaveConnection(slave)
            resultList.append(bytes())
        return ConnectToSlavesResponse(resultList)

    def close(self):
        Logger.info("Lost connection with master")
        self.slaveServer.shutdown()
        return super().close()

    def closeWithError(self, error):
        Logger.info("Closing connection with master: {}".format(error))
        return super().closeWithError(error)


MASTER_OP_NONRPC_MAP = {}


MASTER_OP_RPC_MAP = {
    ClusterOp.CONNECT_TO_SLAVES_REQUEST:
        (ClusterOp.CONNECT_TO_SLAVES_RESPONSE, MasterConnection.handleConnectToSlavesRequest),
    ClusterOp.PING:
        (ClusterOp.PONG, MasterConnection.handlePing),
}


class SlaveConnection(Connection):

    def __init__(self, env, reader, writer, slaveServer, slaveId, shardMaskList):
        super().__init__(env, reader, writer, CLUSTER_OP_SERIALIZER_MAP, SLAVE_OP_NONRPC_MAP, SLAVE_OP_RPC_MAP)
        self.slaveServer = slaveServer
        self.id = slaveId
        self.shardMaskList = shardMaskList

        asyncio.ensure_future(self.activeAndLoopForever())

    def hasShard(self, shardId):
        for shardMask in self.shardMaskList:
            if is_shard_in_mask(shardId, shardMask):
                return True
        return False

    async def sendPing(self):
        req = Ping(self.slaveServer.id, self.slaveServer.shardMaskList)
        op, resp, rpcId = await self.writeRpcRequest(ClusterOp.PING, req)
        return (resp.id, resp.shardMaskList)

    async def handlePing(self, ping):
        if not self.id:
            self.id = ping.id
            self.shardMaskList = ping.shardMaskList
            self.slaveServer.addSlaveConnection(self)
        if len(self.shardMaskList) == 0:
            return self.closeWithError("Empty shard mask list from slave {}".format(self.id))

        return Pong(self.slaveServer.id, self.slaveServer.shardMaskList)

    def closeWithError(self, error):
        Logger.info("Closing connection with slave {}".format(self.id))
        return super().closeWithError(error)


SLAVE_OP_NONRPC_MAP = {}


SLAVE_OP_RPC_MAP = {
    ClusterOp.PING:
        (ClusterOp.PONG, SlaveConnection.handlePing),
}


class SlaveServer():
    """ Slave node in a cluster """

    def __init__(self, env):
        self.loop = asyncio.get_event_loop()
        self.env = env
        self.id = self.env.clusterConfig.ID
        self.shardMaskList = self.env.clusterConfig.SHARD_MASK_LIST

        # shard id -> a list of slave running the shard
        self.shardToSlaves = [[] for i in range(self.__getShardSize())]
        self.slaveConnections = set()
        self.slaveIds = set()

        self.master = None

    def __getShardSize(self):
        return self.env.config.SHARD_SIZE

    def addSlaveConnection(self, slave):
        self.slaveIds.add(slave.id)
        self.slaveConnections.add(slave)
        for shardId in range(self.__getShardSize()):
            if slave.hasShard(shardId):
                self.shardToSlaves[shardId].append(slave)

        self.__logSummary()

    def __logSummary(self):
        for shardId, slaves in enumerate(self.shardToSlaves):
            Logger.info("[{}] is run by slave {}".format(shardId, [s.id for s in slaves]))

    async def __handleNewConnection(self, reader, writer):
        # The first connection should always come from master
        if not self.master:
            self.master = MasterConnection(self.env, reader, writer, self)
            return

        self.slaveConnections.add(SlaveConnection(self.env, reader, writer, self, None, None))

    async def __startServer(self):
        ''' Run the server until shutdown is called '''
        self.server = await asyncio.start_server(
            self.__handleNewConnection, "0.0.0.0", self.env.clusterConfig.NODE_PORT, loop=self.loop)
        Logger.info("Listening on {} for intra-cluster RPC".format(
            self.server.sockets[0].getsockname()))

    def startAndLoop(self):
        self.loop.create_task(self.__startServer())
        try:
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass
        self.shutdown()

    def shutdown(self):
        self.loop.stop()
        self.server.close()
        self.loop.create_task(self.server.wait_closed())


def parse_args():
    parser = argparse.ArgumentParser()
    # P2P port
    parser.add_argument(
        "--server_port", default=DEFAULT_ENV.config.P2P_SERVER_PORT, type=int)
    # Local port for JSON-RPC, wallet, etc
    parser.add_argument(
        "--enable_local_server", default=False, type=bool)
    parser.add_argument(
        "--local_port", default=DEFAULT_ENV.config.LOCAL_SERVER_PORT, type=int)
    # Seed host which provides the list of available peers
    parser.add_argument(
        "--seed_host", default=DEFAULT_ENV.config.P2P_SEED_HOST, type=str)
    parser.add_argument(
        "--seed_port", default=DEFAULT_ENV.config.P2P_SEED_PORT, type=int)
    # Unique Id identifying the node in the cluster
    parser.add_argument(
        "--node_id", default=DEFAULT_ENV.clusterConfig.ID, type=str)
    # Node port for intra-cluster RPC
    parser.add_argument(
        "--node_port", default=DEFAULT_ENV.clusterConfig.NODE_PORT, type=int)
    # TODO: support a list shard masks
    parser.add_argument(
        "--shard_mask", default=1, type=int)
    parser.add_argument("--in_memory_db", default=False)
    parser.add_argument("--db_path", default="./db", type=str)
    parser.add_argument("--log_level", default="info", type=str)
    args = parser.parse_args()

    set_logging_level(args.log_level)

    env = DEFAULT_ENV.copy()
    env.config.P2P_SERVER_PORT = args.server_port
    env.config.P2P_SEED_HOST = args.seed_host
    env.config.P2P_SEED_PORT = args.seed_port
    env.config.LOCAL_SERVER_PORT = args.local_port
    env.config.LOCAL_SERVER_ENABLE = args.enable_local_server

    env.clusterConfig.ID = bytes(args.node_id, "ascii")
    env.clusterConfig.NODE_PORT = args.node_port
    env.clusterConfig.SHARD_MASK_LIST = [args.shard_mask]

    if not args.in_memory_db:
        env.db = PersistentDb(path=args.db_path, clean=True)

    return env


def main():
    env = parse_args()
    env.NETWORK_ID = 1  # testnet

    # qcState = QuarkChainState(env)
    # network = SimpleNetwork(env, qcState)
    # network.start()

    slaveServer = SlaveServer(env)
    slaveServer.startAndLoop()

    Logger.info("Server is shutdown")


if __name__ == '__main__':
    main()
