import os
import time
import asyncio
import tempfile
from unittest import skip
from binascii import hexlify, unhexlify
from typing import List, Optional
from distutils.dir_util import copy_tree, remove_tree

from lbry import Config, Database, RegTestLedger, Transaction, Output, Input
from lbry.crypto.base58 import Base58
from lbry.schema.claim import Stream, Channel
from lbry.schema.support import Support
from lbry.blockchain.script import OutputScript
from lbry.blockchain.lbrycrd import Lbrycrd
from lbry.blockchain.sync import BlockchainSync
from lbry.blockchain.dewies import dewies_to_lbc, lbc_to_dewies
from lbry.constants import CENT, COIN
from lbry.testcase import AsyncioTestCase


#logging.getLogger('lbry.blockchain').setLevel(logging.DEBUG)


class BasicBlockchainTestCase(AsyncioTestCase):

    LBRYCRD_ARGS = '-rpcworkqueue=128',

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.chain = self.make_chain()
        await self.chain.ensure()
        self.addCleanup(self.chain.stop)
        await self.chain.start(*self.LBRYCRD_ARGS)

    @staticmethod
    def make_chain():
        return Lbrycrd.temp_regtest()

    async def make_db(self, chain):
        db_driver = os.environ.get('TEST_DB', 'sqlite')
        if db_driver == 'sqlite':
            db = Database.temp_sqlite_regtest(chain.ledger.conf.lbrycrd_dir)
        elif db_driver.startswith('postgres') or db_driver.startswith('psycopg'):
            db_driver = 'postgresql'
            db_name = f'lbry_test_chain'
            db_connection = 'postgres:postgres@localhost:5432'
            meta_db = Database.from_url(f'postgresql://{db_connection}/postgres')
            await meta_db.drop(db_name)
            await meta_db.create(db_name)
            db = Database.temp_from_url_regtest(f'postgresql://{db_connection}/{db_name}', chain.ledger.conf.lbrycrd_dir)
        else:
            raise RuntimeError(f"Unsupported database driver: {db_driver}")
        self.addCleanup(remove_tree, db.ledger.conf.data_dir)
        await db.open()
        self.addCleanup(db.close)
        self.db_driver = db_driver
        return db

    @staticmethod
    def find_claim_txo(tx) -> Optional[Output]:
        for txo in tx.outputs:
            if txo.is_claim:
                return txo

    @staticmethod
    def find_support_txo(tx) -> Optional[Output]:
        for txo in tx.outputs:
            if txo.is_support:
                return txo


class SyncingBlockchainTestCase(BasicBlockchainTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()

        self.current_height = 0
        await self.generate(101, wait=False)

        self.db = await self.make_db(self.chain)
        self.chain.ledger.conf.spv_address_filters = False
        self.sync = BlockchainSync(self.chain, self.db)
        await self.sync.start()
        self.addCleanup(self.sync.stop)

        self.last_block_hash = None
        self.address = await self.chain.get_new_address()

        self.channel_keys = {}

    async def generate(self, blocks, wait=True) -> List[str]:
        block_hashes = await self.chain.generate(blocks)
        self.current_height += blocks
        self.last_block_hash = block_hashes[-1]
        if wait:
            await self.sync.on_block.where(lambda b: self.current_height == b.height)
        return block_hashes

    async def get_last_block(self):
        return await self.chain.get_block(self.last_block_hash)

    async def get_claim(self, txid: str) -> Output:
        raw = await self.chain.get_raw_transaction(txid)
        tx = Transaction(unhexlify(raw))
        txo = self.find_claim_txo(tx)
        if txo and txo.is_claim and txo.claim.is_channel:
            txo.private_key = self.channel_keys.get(txo.claim_hash)
        return txo

    async def get_support(self, txid: str) -> Output:
        raw = await self.chain.get_raw_transaction(txid)
        return self.find_support_txo(Transaction(unhexlify(raw)))

    async def create_claim(
            self, title='', amount='0.01', name=None, author='', desc='',
            claim_id_startswith='', sign=None, is_channel=False) -> str:
        name = name or ('@foo' if is_channel else 'foo')
        if not claim_id_startswith and sign is None and not is_channel:
            claim = Stream().update(title=title, author=author, description=desc).claim
            return await self.chain.claim_name(
                name, hexlify(claim.to_bytes()).decode(), amount
            )
        meta_class = Channel if is_channel else Stream
        tx = Transaction().add_outputs([
            Output.pay_claim_name_pubkey_hash(
                lbc_to_dewies(amount), name,
                meta_class().update(title='claim #001').claim,
                self.chain.ledger.address_to_hash160(self.address)
            )
        ])
        private_key = None
        if is_channel:
            private_key = await self.find_claim_txo(tx).generate_channel_private_key()
        funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
        tx = Transaction(unhexlify(funded['hex']))
        i = 1
        if '!' in claim_id_startswith:
            claim_id_startswith, not_after_startswith = claim_id_startswith.split('!')
            not_after_startswith = tuple(not_after_startswith)
        else:
            claim_id_startswith, not_after_startswith = claim_id_startswith, ()
        while True:
            if sign:
                self.find_claim_txo(tx).sign(sign)
            tx._reset()
            signed = await self.chain.sign_raw_transaction_with_wallet(hexlify(tx.raw).decode())
            tx = Transaction(unhexlify(signed['hex']))
            txo = self.find_claim_txo(tx)
            claim = txo.claim.channel if is_channel else txo.claim.stream
            if txo.claim_id.startswith(claim_id_startswith):
                if txo.claim_id[len(claim_id_startswith)] not in not_after_startswith:
                    break
            i += 1
            claim.update(title=f'claim #{i:03}')
            txo.script.generate()
        if private_key:
            self.channel_keys[self.find_claim_txo(tx).claim_hash] = private_key
        return await self.chain.send_raw_transaction(hexlify(tx.raw).decode())

    async def update_claim(self, txo: Output, amount='0.01', reset_channel_key=False, sign=None) -> str:
        if reset_channel_key:
            self.channel_keys[txo.claim_hash] = await txo.generate_channel_private_key()
        if sign is None:
            return await self.chain.update_claim(
                txo.tx_ref.id, hexlify(txo.claim.to_bytes()).decode(), amount
            )
        tx = (
            Transaction()
            .add_inputs([Input.spend(txo)])
            .add_outputs([
                Output.pay_update_claim_pubkey_hash(
                    lbc_to_dewies(amount), txo.claim_name, txo.claim_id, txo.claim,
                    self.chain.ledger.address_to_hash160(self.address)
                )
            ])
        )
        funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
        tx = Transaction(unhexlify(funded['hex']))
        self.find_claim_txo(tx).sign(sign)
        tx._reset()
        signed = await self.chain.sign_raw_transaction_with_wallet(hexlify(tx.raw).decode())
        tx = Transaction(unhexlify(signed['hex']))
        return await self.chain.send_raw_transaction(signed['hex'])

    async def abandon_claim(self, txid: str) -> str:
        return await self.chain.abandon_claim(txid, self.address)

    async def support_claim(self, txo: Output, amount='0.01', sign=None) -> str:
        if not sign:
            response = await self.chain.support_claim(
                txo.claim_name, txo.claim_id, amount
            )
            return response['txId']
        tx = (
            Transaction()
            .add_outputs([
                Output.pay_support_data_pubkey_hash(
                    lbc_to_dewies(amount), txo.claim_name, txo.claim_id, Support(),
                    self.chain.ledger.address_to_hash160(self.address)
                )
            ])
        )
        funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
        tx = Transaction(unhexlify(funded['hex']))
        self.find_support_txo(tx).sign(sign)
        tx._reset()
        signed = await self.chain.sign_raw_transaction_with_wallet(hexlify(tx.raw).decode())
        return await self.chain.send_raw_transaction(signed['hex'])

    async def abandon_support(self, txid: str) -> str:
        return await self.chain.abandon_support(txid, self.address)

    async def get_takeovers(self):
        takeovers = []
        for takeover in await self.chain.db.get_takeover():
            takeovers.append({
                'name': takeover['name'],
                'height': takeover['height'],
                'claim_id': hexlify(takeover['claimID'][::-1]).decode()
            })
        return takeovers

    async def advance(self, new_height, ops):
        blocks = (new_height-self.current_height)-1
        if blocks > 0:
            await self.generate(blocks)
        claims = []
        for op in ops:
            if len(op) == 3:
                op_type, value, amount = op
            else:
                (op_type, value), amount = op, None
            if op_type == 'claim':
                txid = await self.create_claim(value, amount)
            elif op_type == 'update':
                txid = await self.update_claim(value, amount)
            elif op_type == 'abandon':
                txid = await self.abandon_claim(value)
            elif op_type == 'support':
                txid = await self.support_claim(value, amount)
            else:
                raise ValueError(f'"{op_type}" is unknown operation')
            claims.append(await self.get_claim(txid))
        await self.generate(1)
        return claims

    async def get_controlling(self):
        for txo in await self.db.search_claims(is_controlling=True):
            return (
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(txo.meta['staked_amount']), txo.meta['takeover_height']
            )

    async def get_active(self):
        controlling = await self.get_controlling()
        active = []
        for txo in await self.db.search_claims(
                activation_height__lte=self.current_height,
                expiration_height__gt=self.current_height):
            if controlling and controlling[0] == txo.claim.stream.title:
                continue
            active.append((
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(txo.meta['staked_amount']), txo.meta['activation_height']
            ))
        return active

    async def get_accepted(self):
        accepted = []
        for txo in await self.db.search_claims(
                activation_height__gt=self.current_height,
                expiration_height__gt=self.current_height):
            accepted.append((
                txo.claim.stream.title, dewies_to_lbc(txo.amount),
                dewies_to_lbc(txo.meta['staked_amount']), txo.meta['activation_height']
            ))
        return accepted

    async def state(self, controlling=None, active=None, accepted=None):
        self.assertEqual(controlling, await self.get_controlling())
        self.assertEqual(active or [], await self.get_active())
        self.assertEqual(accepted or [], await self.get_accepted())


class TestLbrycrdEvents(BasicBlockchainTestCase):

    async def test_block_event(self):
        msgs = []

        self.chain.subscribe()
        self.chain.on_block.listen(lambda e: msgs.append(e['msg']))
        res = await self.chain.generate(5)
        await self.chain.on_block.where(lambda e: e['msg'] == 4)
        self.assertEqual([0, 1, 2, 3, 4], msgs)
        self.assertEqual(5, len(res))

        self.chain.unsubscribe()
        res = await self.chain.generate(2)
        self.assertEqual(2, len(res))
        await asyncio.sleep(0.1)  # give some time to "miss" the new block events

        self.chain.subscribe()
        res = await self.chain.generate(3)
        await self.chain.on_block.where(lambda e: e['msg'] == 9)
        self.assertEqual(3, len(res))
        self.assertEqual([
            0, 1, 2, 3, 4,
            # 5, 6 "missed"
            7, 8, 9
        ], msgs)


class TestMultiBlockFileSyncing(BasicBlockchainTestCase):

    TEST_DATA_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'tmp-lbry-sync-test-data')
    LBRYCRD_ARGS = '-maxblockfilesize=8', '-rpcworkqueue=128'

    def make_chain(self):
        if os.path.exists(self.TEST_DATA_CACHE_DIR):
            temp_dir = tempfile.mkdtemp()
            copy_tree(self.TEST_DATA_CACHE_DIR, temp_dir)
            return Lbrycrd(RegTestLedger(Config.with_same_dir(temp_dir)))
        else:
            return Lbrycrd.temp_regtest()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        generate = not os.path.exists(self.TEST_DATA_CACHE_DIR)

        self.db = await self.make_db(self.chain)
        self.chain.ledger.conf.spv_address_filters = False
        self.sync = BlockchainSync(self.chain, self.db)

        if not generate:
            return

        print(f'generating sample claims... ', end='', flush=True)

        await self.chain.generate(101)

        address = Base58.decode(await self.chain.get_new_address())

        start = time.perf_counter()
        for _ in range(190):
            tx = Transaction().add_outputs([
                Output.pay_claim_name_pubkey_hash(
                    CENT, ["one", "two"][i % 2],
                    Stream().update(
                        title='a claim title',
                        description='Lorem ipsum '*400,
                        tag=['crypto', 'health', 'space'],
                    ).claim,
                    address)
                for i in range(1, 20)
            ])
            funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
            signed = await self.chain.sign_raw_transaction_with_wallet(funded['hex'])
            await self.chain.send_raw_transaction(signed['hex'])
            await self.chain.generate(1)

        # supports \w data aren't supported until block 350, fast forward a little
        await self.chain.generate(60)
        claim = tx.outputs[0]
        tx = Transaction().add_outputs([
            Output.pay_support_pubkey_hash(CENT, claim.claim_name, claim.claim_id, address),
            Output.pay_support_data_pubkey_hash(
                CENT, claim.claim_name, claim.claim_id, Support('🚀'), address
            ),
        ])
        funded = await self.chain.fund_raw_transaction(hexlify(tx.raw).decode())
        signed = await self.chain.sign_raw_transaction_with_wallet(funded['hex'])
        await self.chain.send_raw_transaction(signed['hex'])
        await self.chain.generate(1)

        print(f'took {time.perf_counter()-start}s to generate {190*19} claims ', flush=True)

        await self.chain.stop(False)
        copy_tree(self.chain.ledger.conf.lbrycrd_dir, self.TEST_DATA_CACHE_DIR)
        await self.chain.start(*self.LBRYCRD_ARGS)

    @staticmethod
    def extract_block_events(name, events):
        return sorted([[
            p['data']['block_file'],
            p['data']['step'],
            p['data']['total'],
            p['data']['txs_done'],
            p['data']['txs_total'],
        ] for p in events if p['event'] == name])

    @staticmethod
    def extract_events(name, events):
        return sorted([
            [p['data']['step'], p['data']['total']]
            for p in events if p['event'] == name
        ])

    def assertEventsAlmostEqual(self, actual, expected):
        # this is needed because the sample tx data created
        # by lbrycrd does not have deterministic number of TXIs,
        # which throws off the progress reporting steps.
        # adjust the 'actual' to match 'expected' if it's only off by 1:
        for e, a in zip(expected, actual):
            if a[1] != e[1] and abs(a[1]-e[1]) <= 1:
                a[1] = e[1]
        self.assertEqual(expected, actual)

    async def test_lbrycrd_database_queries(self):
        db = self.chain.db

        # get_best_height
        self.assertEqual(352, await db.get_best_height())

        # get_block_files
        self.assertEqual(
            [(0, 191, 280), (1, 89, 178), (2, 73, 86)],
            [(file['file_number'], file['blocks'], file['txs'])
             for file in await db.get_block_files()]
        )
        self.assertEqual(
            [(1, 29, 58)],
            [(file['file_number'], file['blocks'], file['txs'])
             for file in await db.get_block_files(1, 251)]
        )

        # get_blocks_in_file
        self.assertEqual(279, (await db.get_blocks_in_file(1))[88]['height'])
        self.assertEqual(279, (await db.get_blocks_in_file(1, 251))[28]['height'])

        # get_takeover_count
        self.assertEqual(0, await db.get_takeover_count(0, 100))
        self.assertEqual(3610, await db.get_takeover_count(101, 102))
        self.assertEqual(0, await db.get_takeover_count(103, 1000))

        # get_takeovers
        self.assertEqual(
            [
                {'height': 250, 'name': ''},  # normalization on regtest kicks-in
                {'height': 102, 'name': 'one'},
                {'height': 102, 'name': 'two'},
            ],
            [{'name': takeover['normalized'], 'height': takeover['height']}
             for takeover in await db.get_takeovers(0, 291)]
        )

        # get_claim_metadata_count
        self.assertEqual(3610, await db.get_claim_metadata_count(0, 500))
        self.assertEqual(0, await db.get_claim_metadata_count(500, 1000))

        # get_support_metadata_count
        self.assertEqual(2, await db.get_support_metadata_count(0, 500))
        self.assertEqual(0, await db.get_support_metadata_count(500, 1000))

        # get_support_metadata
        self.assertEqual(
            [{'name': b'two', 'activation_height': 359, 'expiration_height': 852},
             {'name': b'two', 'activation_height': 359, 'expiration_height': 852}],
            [{'name': c['name'], 'activation_height': c['activation_height'], 'expiration_height': c['expiration_height']}
             for c in await db.get_support_metadata(0, 500)]
        )

    def assertConsumingEvents(self, events: list, name, units, expectation_generator):
        expected_count = 0
        for expectation in expectation_generator:
            expected_count += len(expectation[2:])
        self.assertGreaterEqual(len(events), expected_count)
        extracted = []
        for _ in range(expected_count):
            extracted.append(events.pop(0))
        actual = sorted(extracted, key=lambda e: (e["event"], e["data"]["id"], e["data"]["done"]))
        expected = []
        for expectation in expectation_generator:
            for i, done in enumerate(expectation[2:]):
                if i == 0:
                    first_event = {
                        "event": name,
                        "data": {
                            "id": expectation[0],
                            "done": (0,) * len(units),
                            "total": expectation[2],
                            "units": units,
                        }
                    }
                    if expectation[1] is not None:
                        first_event["data"]["label"] = expectation[1]
                    expected.append(first_event)
                else:
                    expected.append({
                        "event": name,
                        "data": {
                            "id": expectation[0],
                            "done": done,
                        }
                    })
        self.assertEqual(expected, actual)

    async def test_multi_block_file_sync(self):
        events = []
        self.sync.on_progress.listen(events.append)

        # initial_sync = True
        await self.sync.advance()
        await asyncio.sleep(1)  # give it time to collect events
        self.assertConsumingEvents(
            events, "blockchain.sync.block.init", ("steps",), [
                (0, None, (3,), (1,), (2,), (3,))
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.block.main",
                "data": {
                    "id": 0, "done": (0, 0), "total": (353, 544), "units": ("blocks", "txs"),
                    "starting_height": 0, "ending_height": 352,
                    "files": 3, "claims": 3610, "supports": 2
                }
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.block.file", ("blocks", "txs"), [
                (0, "blk00000.dat", (191, 280), (100, 0), (191, 280)),
                (1, "blk00001.dat", (89, 178), (89, 178)),
                (2, "blk00002.dat", (73, 86), (73, 86)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.block.main",
                "data": {"id": 0, "done": (-1, -1)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.txoi.main", ("steps",), [
                (0, None, (9,), (1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,), (9,))
            ]
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.init", ("steps",), [
                (0, None, (2,), (1,), (2,))
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.claims.main",
                "data": {"id": 0, "done": (0,), "total": (3610,), "units": ("claims",)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.insert", ("claims",), [
                (102, "add claims at 102-120", (361,), (361,)),
                (121, "add claims at 121-139", (361,), (361,)),
                (140, "add claims at 140-158", (361,), (361,)),
                (159, "add claims at 159-177", (361,), (361,)),
                (178, "add claims at 178-196", (361,), (361,)),
                (197, "add claims at 197-215", (361,), (361,)),
                (216, "add claims at 216-234", (361,), (361,)),
                (235, "add claims at 235-253", (361,), (361,)),
                (254, "add claims at 254-272", (361,), (361,)),
                (273, "add claims at 273-291", (361,), (361,)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.claims.main",
                "data": {"id": 0, "done": (-1,)}
            }
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.supports.main",
                "data": {"id": 0, "done": (0,), "total": (2,), "units": ("supports",)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.supports.insert", ("supports",), [
                (352, "add supports at 352", (2,), (2,)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.supports.main",
                "data": {"id": 0, "done": (-1,)}
            }
        )
        self.assertEqual(events, [])

        # initial_sync = False & no new blocks
        await self.sync.advance()  # should be no-op
        await asyncio.sleep(1)  # give it time to collect events
        self.assertConsumingEvents(
            events, "blockchain.sync.block.init", ("steps",), [
                (0, None, (3,), (1,), (2,), (3,))
            ]
        )
        self.assertEqual(events, [])

        # initial_sync = False
        txid = await self.chain.claim_name('foo', 'beef', '0.01')
        await self.chain.generate(1)
        tx = Transaction(unhexlify(await self.chain.get_raw_transaction(txid)))
        txo = self.find_claim_txo(tx)
        await self.chain.support_claim('foo', txo.claim_id, '0.01')
        await self.chain.generate(1)
        await self.sync.advance()
        await asyncio.sleep(1)  # give it time to collect events
        self.assertConsumingEvents(
            events, "blockchain.sync.block.init", ("steps",), [
                (0, None, (3,), (1,), (2,), (3,))
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.block.main",
                "data": {
                    "id": 0, "done": (0, 0), "total": (2, 4), "units": ("blocks", "txs"),
                    "starting_height": 353, "ending_height": 354,
                    "files": 1, "claims": 1, "supports": 1
                }
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.block.file", ("blocks", "txs"), [
                (2, "blk00002.dat", (2, 4), (2, 4)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.block.main",
                "data": {"id": 0, "done": (-1, -1)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.txoi.main", ("steps",), [
                (0, None, (2,), (1,), (2,))
            ]
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.init", ("steps",), [
                (0, None, (6,), (1,), (2,), (3,), (4,), (5,), (6,))
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.claims.main",
                "data": {"id": 0, "done": (0,), "total": (3,), "units": ("claims",)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.insert", ("claims",), [
                (353, "add claims at 353-354", (1,), (1,)),
            ]
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.takeovers", ("claims",), [
                (0, "winning", (1,), (1,)),
            ]
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.claims.stakes", ("claims",), [
                (0, None, (1,), (1,)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.claims.main",
                "data": {"id": 0, "done": (-1,)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.supports.init", ("steps",), [
                (0, None, (2,), (1,), (2,))
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.supports.main",
                "data": {"id": 0, "done": (0,), "total": (1,), "units": ("supports",)}
            }
        )
        self.assertConsumingEvents(
            events, "blockchain.sync.supports.insert", ("supports",), [
                (353, "add supports at 353-354", (1,), (1,)),
            ]
        )
        self.assertEqual(
            events.pop(0), {
                "event": "blockchain.sync.supports.main",
                "data": {"id": 0, "done": (-1,)}
            }
        )
        self.assertEqual(events, [])


class TestGeneralBlockchainSync(SyncingBlockchainTestCase):

    async def test_sync_advances(self):
        blocks = []
        self.sync.on_block.listen(blocks.append)
        await self.generate(1)
        await self.generate(1)
        await self.generate(1)
        self.assertEqual([102, 103, 104], [b.height for b in blocks])
        self.assertEqual(104, self.current_height)
        blocks.clear()
        await self.generate(6)
        self.assertEqual([110], [b.height for b in blocks])
        self.assertEqual(110, self.current_height)

    async def test_claim_create_update_and_delete(self):
        search = self.db.search_claims
        await self.create_claim('foo', '0.01')
        await self.generate(1)
        claims = await search()
        self.assertEqual(1, len(claims))
        self.assertEqual(claims[0].claim_name, 'foo')
        self.assertEqual(dewies_to_lbc(claims[0].amount), '0.01')
        await self.support_claim(claims[0], '0.08')
        await self.support_claim(claims[0], '0.03')
        await self.update_claim(claims[0], '0.02')
        await self.generate(1)
        claims = await search()
        self.assertEqual(1, len(claims))
        self.assertEqual(claims[0].claim_name, 'foo')
        self.assertEqual(dewies_to_lbc(claims[0].amount), '0.02')
        self.assertEqual(dewies_to_lbc(claims[0].meta['staked_amount']), '0.13')
        self.assertEqual(dewies_to_lbc(claims[0].meta['staked_support_amount']), '0.11')
        await self.abandon_claim(claims[0].tx_ref.id)
        await self.generate(1)
        claims = await search()
        self.assertEqual(0, len(claims))

    async def test_nulls(self):
        await self.create_claim(name='\x00', title='\x00', author='\x00', desc='\x00')
        await self.generate(1)
        empty_name, = await self.db.search_claims()
        self.assertEqual('', empty_name.normalized_name)

    async def test_claim_in_abandoned_channel(self):
        await self.sync.stop()
        channel_1 = await self.get_claim(await self.create_claim(is_channel=True))
        channel_2 = await self.get_claim(await self.create_claim(is_channel=True))
        await self.generate(1, wait=False)
        await self.create_claim(sign=channel_1)
        await self.generate(1, wait=False)
        await self.create_claim(sign=channel_2)
        await self.generate(1, wait=False)
        await self.abandon_claim(channel_1.tx_ref.id)
        await self.generate(1, wait=False)
        await self.sync.start()
        c2, c1 = await self.db.search_claims(order_by=['height'], claim_type='stream')
        self.assertEqual(c1.meta['is_signature_valid'], True)  # valid at time of pubulish
        self.assertIsNone(c1.meta['canonical_url'], None)  # channel is abandoned
        self.assertEqual(c2.meta['is_signature_valid'], True)
        self.assertIsNotNone(c2.meta['canonical_url'])

    async def test_short_and_canonical_urls(self):
        search = self.db.search_claims

        # same block (no claim gets preference, therefore both end up with short hash of len 2)
        await self.create_claim(claim_id_startswith='a1')
        await self.create_claim(claim_id_startswith='a2')
        await self.generate(1)
        a2, a1 = await search(order_by=['claim_id'], limit=2)
        self.assertEqual("foo#a1", a1.meta['short_url'])
        self.assertEqual("foo#a2", a2.meta['short_url'])
        self.assertIsNone(a1.meta['canonical_url'])
        self.assertIsNone(a2.meta['canonical_url'])

        # separate blocks (first claim had no competition, so it got very short url, second got longer)
        await self.create_claim(claim_id_startswith='b1')
        await self.generate(1)
        await self.create_claim(claim_id_startswith='b2')
        await self.generate(1)
        b2, b1 = await search(order_by=['claim_id'], limit=2)
        self.assertEqual("foo#b", b1.meta['short_url'])
        self.assertEqual("foo#b2", b2.meta['short_url'])
        self.assertIsNone(b1.meta['canonical_url'])
        self.assertIsNone(b2.meta['canonical_url'])

        # channels also have urls
        channel_a1 = await self.get_claim(
            await self.create_claim(claim_id_startswith='a1', is_channel=True))
        await self.generate(1)
        channel_a2 = await self.get_claim(
            await self.create_claim(claim_id_startswith='a2', is_channel=True))
        await self.generate(1)
        chan_a2, chan_a1 = await search(order_by=['claim_id'], claim_type="channel", limit=2)
        self.assertEqual("@foo#a", chan_a1.meta['short_url'])
        self.assertEqual("@foo#a2", chan_a2.meta['short_url'])
        self.assertIsNone(chan_a1.meta['canonical_url'])
        self.assertIsNone(chan_a2.meta['canonical_url'])

        # signing a new claim and signing as an update
        await self.create_claim(claim_id_startswith='c', sign=channel_a1)
        await self.update_claim(b2, sign=channel_a2)
        await self.generate(1)
        c, b2 = await search(order_by=['claim_id'], claim_type='stream', limit=2)
        self.assertEqual("@foo#a/foo#c", c.meta['canonical_url'])
        self.assertEqual("@foo#a2/foo#b2", b2.meta['canonical_url'])

        # changing previously set channel
        await self.update_claim(c, sign=channel_a2)
        await self.generate(1)
        c, = await search(order_by=['claim_id'], claim_type='stream', limit=1)
        self.assertEqual("@foo#a2/foo#c", c.meta['canonical_url'])

    async def assert_channel_stream1_stream2_support(
            self,
            signed_claim_count=0,
            signed_support_count=0,
            stream1_valid=False,
            stream1_channel=None,
            stream2_valid=False,
            stream2_channel=None,
            support_valid=False,
            support_channel=None):
        search = self.db.search_claims

        r, = await search(claim_id=self.stream1.claim_id)
        self.assertEqual(r.meta['is_signature_valid'], stream1_valid)
        if stream1_channel is None:
            self.assertIsNone(r.claim.signing_channel_id)
        else:
            self.assertEqual(r.claim.signing_channel_id, stream1_channel.claim_id)

        r, = await search(claim_id=self.stream2.claim_id)
        self.assertEqual(r.meta['is_signature_valid'], stream2_valid)
        if stream2_channel is None:
            self.assertIsNone(r.claim.signing_channel_id)
        else:
            self.assertEqual(r.claim.signing_channel_id, stream2_channel.claim_id)

        r, = await search(claim_id=self.channel.claim_id)
        self.assertEqual(signed_claim_count, r.meta['signed_claim_count'])
        self.assertEqual(signed_support_count, r.meta['signed_support_count'])

        if support_channel is not None:
            r, = await self.db.search_supports()
            self.assertEqual(r.meta['is_signature_valid'], support_valid)
            self.assertEqual(r.support.signing_channel_id, support_channel.claim_id)

    async def test_claim_and_support_signing(self):
        search = self.db.search_claims

        # create a stream not in channel, should not have signature
        self.stream1 = await self.get_claim(
            await self.create_claim())
        await self.generate(1)
        r, = await search(claim_type='stream')
        self.assertFalse(r.claim.is_signed)
        self.assertFalse(r.meta['is_signature_valid'])
        self.assertIsNone(r.claim.signing_channel_id)

        # create a new channel, should not have claims or supports
        self.channel = await self.get_claim(
            await self.create_claim(is_channel=True))
        await self.generate(1)
        r, = await search(claim_type='channel')
        self.assertEqual(0, r.meta['signed_claim_count'])
        self.assertEqual(0, r.meta['signed_support_count'])

        # create a signed claim, update unsigned claim to have signature and create a signed support
        self.stream2 = await self.get_claim(
            await self.create_claim(sign=self.channel))
        await self.update_claim(self.stream1, sign=self.channel)
        self.support = await self.get_support(
            await self.support_claim(self.stream1, sign=self.channel))
        await self.generate(1)

        await self.assert_channel_stream1_stream2_support(
            signed_claim_count=2, signed_support_count=1,
            stream1_valid=True, stream1_channel=self.channel,
            stream2_valid=True, stream2_channel=self.channel,
            support_valid=True, support_channel=self.channel
        )

        # resetting channel key doesn't invalidate previously published streams
        await self.update_claim(self.channel, reset_channel_key=True)
        await self.generate(1)

        await self.assert_channel_stream1_stream2_support(
            signed_claim_count=2, signed_support_count=1,
            stream1_valid=True, stream1_channel=self.channel,
            stream2_valid=True, stream2_channel=self.channel,
            support_valid=True, support_channel=self.channel
        )

        # updating a claim with an invalid signature marks signature invalid
        await self.channel.generate_channel_private_key()  # new key but no broadcast of change
        self.stream2 = await self.get_claim(
            await self.update_claim(self.stream2, sign=self.channel))
        await self.generate(1)

        await self.assert_channel_stream1_stream2_support(
            signed_claim_count=1, signed_support_count=1,  # channel lost one signed claim
            stream1_valid=True, stream1_channel=self.channel,
            stream2_valid=False, stream2_channel=self.channel,  # sig invalid
            support_valid=True, support_channel=self.channel
        )

        # updating it again with correct signature fixes it
        self.channel = await self.get_claim(self.channel.tx_ref.id)  # get original channel
        self.stream2 = await self.get_claim(
            await self.update_claim(self.stream2, sign=self.channel))
        await self.generate(1)

        await self.assert_channel_stream1_stream2_support(
            signed_claim_count=2, signed_support_count=1,  # channel re-gained claim
            stream1_valid=True, stream1_channel=self.channel,
            stream2_valid=True, stream2_channel=self.channel,  # sig valid now
            support_valid=True, support_channel=self.channel
        )

        # sign stream with a different channel
        self.channel2 = await self.get_claim(
            await self.create_claim(is_channel=True))
        self.stream2 = await self.get_claim(
            await self.update_claim(self.stream2, sign=self.channel2))
        await self.generate(1)

        await self.assert_channel_stream1_stream2_support(
            signed_claim_count=1, signed_support_count=1,  # channel1 lost a claim
            stream1_valid=True, stream1_channel=self.channel,
            stream2_valid=True, stream2_channel=self.channel2,  # new channel is the valid signer
            support_valid=True, support_channel=self.channel
        )
        r, = await search(claim_id=self.channel2.claim_id)
        self.assertEqual(1, r.meta['signed_claim_count'])  # channel2 gained a claim
        self.assertEqual(0, r.meta['signed_support_count'])

        # deleting claim and support
        await self.abandon_claim(self.stream2.tx_ref.id)
        await self.abandon_support(self.support.tx_ref.id)
        await self.generate(1)
        r, = await search(claim_id=self.channel.claim_id)
        self.assertEqual(1, r.meta['signed_claim_count'])
        self.assertEqual(0, r.meta['signed_support_count'])  # channel1 lost abandoned support
        r, = await search(claim_id=self.channel2.claim_id)
        self.assertEqual(0, r.meta['signed_claim_count'])  # channel2 lost abandoned claim
        self.assertEqual(0, r.meta['signed_support_count'])

    async def resolve_to_claim_id(self, url):
        return (await self.db.resolve(url))[url].claim_id

    async def test_resolve(self):
        chan_a = await self.get_claim(
            await self.create_claim(claim_id_startswith='a!b', is_channel=True))
        await self.generate(1)
        chan_ab = await self.get_claim(
            await self.create_claim(claim_id_startswith='ab', is_channel=True))
        await self.generate(1)
        self.assertEqual(chan_a.claim_id, await self.resolve_to_claim_id("@foo#a"))
        self.assertEqual(chan_ab.claim_id, await self.resolve_to_claim_id("@foo#ab"))

        stream_c = await self.get_claim(
            await self.create_claim(claim_id_startswith='c!d', sign=chan_a))
        await self.generate(1)
        stream_cd = await self.get_claim(
            await self.create_claim(claim_id_startswith='cd', sign=chan_ab))
        await self.generate(1)
        self.assertEqual(stream_c.claim_id, await self.resolve_to_claim_id("@foo#a/foo#c"))
        self.assertEqual(stream_cd.claim_id, await self.resolve_to_claim_id("@foo#ab/foo#cd"))


class TestClaimtrieSync(SyncingBlockchainTestCase):

    async def test_example_from_spec(self):
        # https://spec.lbry.com/#claim-activation-example
        advance, state = self.advance, self.state
        stream, = await advance(113, [('claim', 'Claim A', '10.0')])
        await state(
            controlling=('Claim A', '10.0', '10.0', 113),
            active=[],
            accepted=[]
        )
        await advance(501, [('claim', 'Claim B', '20.0')])
        await state(
            controlling=('Claim A', '10.0', '10.0', 113),
            active=[],
            accepted=[('Claim B', '20.0', '20.0', 513)]
        )
        await advance(510, [('support', stream, '14.0')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[],
            accepted=[('Claim B', '20.0', '20.0', 513)]
        )
        await advance(512, [('claim', 'Claim C', '50.0')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[],
            accepted=[
                ('Claim B', '20.0', '20.0', 513),
                ('Claim C', '50.0', '50.0', 524)]
        )
        await advance(513, [])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[('Claim B', '20.0', '20.0', 513)],
            accepted=[('Claim C', '50.0', '50.0', 524)]
        )
        await advance(520, [('claim', 'Claim D', '60.0')])
        await state(
            controlling=('Claim A', '10.0', '24.0', 113),
            active=[('Claim B', '20.0', '20.0', 513)],
            accepted=[
                ('Claim C', '50.0', '50.0', 524),
                ('Claim D', '60.0', '60.0', 532)]
        )
        await advance(524, [])
        await state(
            controlling=('Claim D', '60.0', '60.0', 524),
            active=[
                ('Claim A', '10.0', '24.0', 113),
                ('Claim B', '20.0', '20.0', 513),
                ('Claim C', '50.0', '50.0', 524)],
            accepted=[]
        )
        # beyond example
        await advance(525, [('update', stream, '70.0')])
        await state(
            controlling=('Claim A', '70.0', '84.0', 525),
            active=[
                ('Claim B', '20.0', '20.0', 513),
                ('Claim C', '50.0', '50.0', 524),
                ('Claim D', '60.0', '60.0', 524),
            ],
            accepted=[]
        )

    async def test_competing_claims_subsequent_blocks_height_wins(self):
        advance, state = self.advance, self.state
        await advance(113, [('claim', 'Claim A', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[],
            accepted=[]
        )
        await advance(114, [('claim', 'Claim B', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[('Claim B', '1.0', '1.0', 114)],
            accepted=[]
        )
        await advance(115, [('claim', 'Claim C', '1.0')])
        await state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[
                ('Claim B', '1.0', '1.0', 114),
                ('Claim C', '1.0', '1.0', 115)],
            accepted=[]
        )

    async def test_competing_claims_in_single_block_position_wins(self):
        claim_a, claim_b = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '1.0')
        ])
        block = await self.get_last_block()
        # order of tx in block is non-deterministic,
        # figure out what ordered we ended up with
        if block['tx'][1] == claim_a.tx_ref.id:
            winner, other = 'Claim A', 'Claim B'
        else:
            winner, other = 'Claim B', 'Claim A'
        await self.state(
            controlling=(winner, '1.0', '1.0', 113),
            active=[(other, '1.0', '1.0', 113)],
            accepted=[]
        )

    async def test_competing_claims_in_single_block_effective_amount_wins(self):
        await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )

    async def winning_claim_deleted_test(self):
        claim1, claim2 = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )
        await self.advance(114, [('abandon', claim2)])
        await self.state(
            controlling=('Claim A', '1.0', '1.0', 113),
            active=[],
            accepted=[]
        )

    async def test_winning_claim_deleted_and_new_claim_becomes_winner(self):
        claim1, claim2 = await self.advance(113, [
            ('claim', 'Claim A', '1.0'),
            ('claim', 'Claim B', '2.0')
        ])
        await self.state(
            controlling=('Claim B', '2.0', '2.0', 113),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )
        await self.advance(115, [
            ('abandon', claim2.tx_ref.id),
            ('claim', 'Claim C', '3.0')
        ])
        await self.state(
            controlling=('Claim C', '3.0', '3.0', 115),
            active=[('Claim A', '1.0', '1.0', 113)],
            accepted=[]
        )

    async def test_winning_claim_expires_and_another_takes_over(self):
        await self.advance(110, [('claim', 'Claim A', '2.0')])
        await self.advance(120, [('claim', 'Claim B', '1.0')])
        await self.state(
            controlling=('Claim A', '2.0', '2.0', 110),
            active=[('Claim B', '1.0', '1.0', 120)],
            accepted=[]
        )
        await self.advance(610, [])
        await self.state(
            controlling=('Claim B', '1.0', '1.0', 610),
            active=[],
            accepted=[]
        )
        await self.advance(620, [])
        await self.state(
            controlling=None,
            active=[],
            accepted=[]
        )

    async def test_create_and_multiple_updates_in_same_block(self):
        await self.generate(10)
        txid = await self.create_claim('Claim A', '1.0')
        txid = await self.update_claim(await self.get_claim(txid), '2.0')
        await self.update_claim(await self.get_claim(txid), '3.0')
        await self.generate(1)
        await self.sync.advance()
        await self.state(
            controlling=('Claim A', '3.0', '3.0', 112),
            active=[],
            accepted=[]
        )

    async def test_create_and_abandon_in_same_block(self):
        await self.generate(10)
        txid = await self.create_claim('Claim A', '1.0')
        await self.abandon_claim(txid)
        await self.generate(1)
        await self.sync.advance()
        await self.state(
            controlling=None,
            active=[],
            accepted=[]
        )


@skip
class TestTrending(SyncingBlockchainTestCase):

    def test_trending(self):
        advance = self.advance
        no_trend = self.get_stream('Claim A', COIN)
        downwards = self.get_stream('Claim B', COIN)
        up_small = self.get_stream('Claim C', COIN)
        up_medium = self.get_stream('Claim D', COIN)
        up_biggly = self.get_stream('Claim E', COIN)
        claims = advance(1, [up_biggly, up_medium, up_small, no_trend, downwards])
        for window in range(1, 8):
            advance(zscore.TRENDING_WINDOW * window, [
                self.get_support(downwards, (20-window)*COIN),
                self.get_support(up_small, int(20+(window/10)*COIN)),
                self.get_support(up_medium, (20+(window*(2 if window == 7 else 1)))*COIN),
                self.get_support(up_biggly, (20+(window*(3 if window == 7 else 1)))*COIN),
            ])
        results = search(order_by=['trending_local'])
        self.assertEqual([c.claim_id for c in claims], [hexlify(c['claim_hash'][::-1]).decode() for c in results])
        self.assertEqual([10, 6, 2, 0, -2], [int(c['trending_local']) for c in results])
        self.assertEqual([53, 38, -32, 0, -6], [int(c['trending_global']) for c in results])
        self.assertEqual([4, 4, 2, 0, 1], [int(c['trending_group']) for c in results])
        self.assertEqual([53, 38, 2, 0, -6], [int(c['trending_mixed']) for c in results])

    def test_edge(self):
        problematic = self.get_stream('Problem', COIN)
        self.advance(1, [problematic])
        self.advance(zscore.TRENDING_WINDOW, [self.get_support(problematic, 53000000000)])
        self.advance(zscore.TRENDING_WINDOW * 2, [self.get_support(problematic, 500000000)])


@skip
class TestContentBlocking(SyncingBlockchainTestCase):

    def test_blocking_and_filtering(self):
        # content claims and channels
        tx0 = self.get_channel('A Channel', COIN, '@channel1')
        regular_channel = tx0[0].outputs[0]
        tx1 = self.get_stream('Claim One', COIN, 'claim1')
        tx2 = self.get_stream('Claim Two', COIN, 'claim2', regular_channel)
        tx3 = self.get_stream('Claim Three', COIN, 'claim3')
        self.advance(1, [tx0, tx1, tx2, tx3])
        claim1, claim2, claim3 = tx1[0].outputs[0], tx2[0].outputs[0], tx3[0].outputs[0]

        # block and filter channels
        tx0 = self.get_channel('Blocking Channel', COIN, '@block')
        tx1 = self.get_channel('Filtering Channel', COIN, '@filter')
        blocking_channel = tx0[0].outputs[0]
        filtering_channel = tx1[0].outputs[0]
        self.sql.blocking_channel_hashes.add(blocking_channel.claim_hash)
        self.sql.filtering_channel_hashes.add(filtering_channel.claim_hash)
        self.advance(2, [tx0, tx1])
        self.assertEqual({}, dict(self.sql.blocked_streams))
        self.assertEqual({}, dict(self.sql.blocked_channels))
        self.assertEqual({}, dict(self.sql.filtered_streams))
        self.assertEqual({}, dict(self.sql.filtered_channels))

        # nothing blocked
        results, _ = reader.resolve([
            claim1.claim_name, claim2.claim_name,
            claim3.claim_name, regular_channel.claim_name
        ])
        self.assertEqual(claim1.claim_hash, results[0]['claim_hash'])
        self.assertEqual(claim2.claim_hash, results[1]['claim_hash'])
        self.assertEqual(claim3.claim_hash, results[2]['claim_hash'])
        self.assertEqual(regular_channel.claim_hash, results[3]['claim_hash'])

        # nothing filtered
        results, censor = censored_search()
        self.assertEqual(6, len(results))
        self.assertEqual(0, censor.total)
        self.assertEqual({}, censor.censored)

        # block claim reposted to blocking channel, also gets filtered
        repost_tx1 = self.get_repost(claim1.claim_id, COIN, blocking_channel)
        repost1 = repost_tx1[0].outputs[0]
        self.advance(3, [repost_tx1])
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.blocked_streams)
        )
        self.assertEqual({}, dict(self.sql.blocked_channels))
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.filtered_streams)
        )
        self.assertEqual({}, dict(self.sql.filtered_channels))

        # claim is blocked from results by direct repost
        results, censor = censored_search(text='Claim')
        self.assertEqual(2, len(results))
        self.assertEqual(claim2.claim_hash, results[0]['claim_hash'])
        self.assertEqual(claim3.claim_hash, results[1]['claim_hash'])
        self.assertEqual(1, censor.total)
        self.assertEqual({blocking_channel.claim_hash: 1}, censor.censored)
        results, _ = reader.resolve([claim1.claim_name])
        self.assertEqual(
            f"Resolve of 'claim1' was censored by channel with claim id '{blocking_channel.claim_id}'.",
            results[0].args[0]
        )
        results, _ = reader.resolve([
            claim2.claim_name, regular_channel.claim_name  # claim2 and channel still resolved
        ])
        self.assertEqual(claim2.claim_hash, results[0]['claim_hash'])
        self.assertEqual(regular_channel.claim_hash, results[1]['claim_hash'])

        # block claim indirectly by blocking its parent channel
        repost_tx2 = self.get_repost(regular_channel.claim_id, COIN, blocking_channel)
        repost2 = repost_tx2[0].outputs[0]
        self.advance(4, [repost_tx2])
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.blocked_streams)
        )
        self.assertEqual(
            {repost2.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.blocked_channels)
        )
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.filtered_streams)
        )
        self.assertEqual(
            {repost2.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.filtered_channels)
        )

        # claim in blocked channel is filtered from search and can't resolve
        results, censor = censored_search(text='Claim')
        self.assertEqual(1, len(results))
        self.assertEqual(claim3.claim_hash, results[0]['claim_hash'])
        self.assertEqual(2, censor.total)
        self.assertEqual({blocking_channel.claim_hash: 2}, censor.censored)
        results, _ = reader.resolve([
            claim2.claim_name, regular_channel.claim_name  # claim2 and channel don't resolve
        ])
        self.assertEqual(
            f"Resolve of 'claim2' was censored by channel with claim id '{blocking_channel.claim_id}'.",
            results[0].args[0]
        )
        self.assertEqual(
            f"Resolve of '@channel1' was censored by channel with claim id '{blocking_channel.claim_id}'.",
            results[1].args[0]
        )
        results, _ = reader.resolve([claim3.claim_name])  # claim3 still resolved
        self.assertEqual(claim3.claim_hash, results[0]['claim_hash'])

        # filtered claim is only filtered and not blocked
        repost_tx3 = self.get_repost(claim3.claim_id, COIN, filtering_channel)
        repost3 = repost_tx3[0].outputs[0]
        self.advance(5, [repost_tx3])
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.blocked_streams)
        )
        self.assertEqual(
            {repost2.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.blocked_channels)
        )
        self.assertEqual(
            {repost1.claim.repost.reference.claim_hash: blocking_channel.claim_hash,
             repost3.claim.repost.reference.claim_hash: filtering_channel.claim_hash},
            dict(self.sql.filtered_streams)
        )
        self.assertEqual(
            {repost2.claim.repost.reference.claim_hash: blocking_channel.claim_hash},
            dict(self.sql.filtered_channels)
        )

        # filtered claim doesn't return in search but is resolveable
        results, censor = censored_search(text='Claim')
        self.assertEqual(0, len(results))
        self.assertEqual(3, censor.total)
        self.assertEqual({blocking_channel.claim_hash: 2, filtering_channel.claim_hash: 1}, censor.censored)
        results, _ = reader.resolve([claim3.claim_name])  # claim3 still resolved
        self.assertEqual(claim3.claim_hash, results[0]['claim_hash'])

        # abandon unblocks content
        self.advance(6, [
            self.get_abandon(repost_tx1),
            self.get_abandon(repost_tx2),
            self.get_abandon(repost_tx3)
        ])
        self.assertEqual({}, dict(self.sql.blocked_streams))
        self.assertEqual({}, dict(self.sql.blocked_channels))
        self.assertEqual({}, dict(self.sql.filtered_streams))
        self.assertEqual({}, dict(self.sql.filtered_channels))
        results, censor = censored_search(text='Claim')
        self.assertEqual(3, len(results))
        self.assertEqual(0, censor.total)
        results, censor = censored_search()
        self.assertEqual(6, len(results))
        self.assertEqual(0, censor.total)
        results, _ = reader.resolve([
            claim1.claim_name, claim2.claim_name,
            claim3.claim_name, regular_channel.claim_name
        ])
        self.assertEqual(claim1.claim_hash, results[0]['claim_hash'])
        self.assertEqual(claim2.claim_hash, results[1]['claim_hash'])
        self.assertEqual(claim3.claim_hash, results[2]['claim_hash'])
        self.assertEqual(regular_channel.claim_hash, results[3]['claim_hash'])

    def test_pagination(self):
        one, two, three, four, five, six, seven, filter_channel = self.advance(1, [
            self.get_stream('One', COIN),
            self.get_stream('Two', COIN),
            self.get_stream('Three', COIN),
            self.get_stream('Four', COIN),
            self.get_stream('Five', COIN),
            self.get_stream('Six', COIN),
            self.get_stream('Seven', COIN),
            self.get_channel('Filtering Channel', COIN, '@filter'),
        ])
        self.sql.filtering_channel_hashes.add(filter_channel.claim_hash)

        # nothing filtered
        results, censor = censored_search(order_by='^height', offset=1, limit=3)
        self.assertEqual(3, len(results))
        self.assertEqual(
            [two.claim_hash, three.claim_hash, four.claim_hash],
            [r['claim_hash'] for r in results]
        )
        self.assertEqual(0, censor.total)

        # content filtered
        repost1, repost2 = self.advance(2, [
            self.get_repost(one.claim_id, COIN, filter_channel),
            self.get_repost(two.claim_id, COIN, filter_channel),
        ])
        results, censor = censored_search(order_by='^height', offset=1, limit=3)
        self.assertEqual(3, len(results))
        self.assertEqual(
            [four.claim_hash, five.claim_hash, six.claim_hash],
            [r['claim_hash'] for r in results]
        )
        self.assertEqual(2, censor.total)
        self.assertEqual({filter_channel.claim_hash: 2}, censor.censored)