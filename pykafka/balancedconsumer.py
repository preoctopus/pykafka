from __future__ import division
"""
Author: Emmett Butler
"""
__license__ = """
Copyright 2015 Parse.ly, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
__all__ = ["BalancedConsumer"]
import itertools
import logging as log
import math
import socket
import time
from uuid import uuid4

from kazoo.client import KazooClient
from kazoo.exceptions import NoNodeException, NodeExistsError
from kazoo.recipe.watchers import ChildrenWatch

from .common import OffsetType
from .exceptions import KafkaException
from .simpleconsumer import SimpleConsumer


class BalancedConsumer():
    """
    Maintains a single instance of SimpleConsumer, periodically using the
    consumer rebalancing algorithm to reassign partitions to this
    SimpleConsumer.
    """
    def __init__(self,
                 topic,
                 cluster,
                 consumer_group,
                 fetch_message_max_bytes=1024 * 1024,
                 num_consumer_fetchers=1,
                 auto_commit_enable=False,
                 auto_commit_interval_ms=60 * 1000,
                 queued_max_messages=2000,
                 fetch_min_bytes=1,
                 fetch_wait_max_ms=100,
                 refresh_leader_backoff_ms=200,
                 offsets_channel_backoff_ms=1000,
                 offsets_commit_max_retries=5,
                 auto_offset_reset=OffsetType.LATEST,
                 consumer_timeout_ms=-1,
                 rebalance_max_retries=5,
                 rebalance_backoff_ms=2 * 1000,
                 zookeeper_connection_timeout_ms=6 * 1000,
                 zookeeper_connect='127.0.0.1:2181',
                 zookeeper=None,
                 auto_start=True):
        """Create a BalancedConsumer instance

        :param topic: The topic this consumer should consume
        :type topic: :class:`pykafka.topic.Topic`
        :param cluster: The cluster to which this consumer should connect
        :type cluster: :class:`pykafka.cluster.Cluster`
        :param consumer_group: The name of the consumer group to join
        :type consumer_group: str
        :param fetch_message_max_bytes: The number of bytes of messages to
            attempt to fetch with each fetch request
        :type fetch_message_max_bytes: int
        :param num_consumer_fetchers: The number of threads used to fetch data
        :type num_consumer_fetchers: int
        :param auto_commit_enable: If true, periodically commit to kafka the
            offset of messages already fetched by this consumer. This also
            requires that consumer_group is not None.
        :type auto_commit_enable: bool
        :param auto_commit_interval_ms: The frequency (in milliseconds) at which
            the consumer's offsets are committed to kafka. This setting is
            ignored if auto_commit_enable is False.
        :type auto_commit_interval_ms: int
        :param queued_max_messages: The maximum number of messages buffered for
            consumption in the internal
            :class:`pykafka.simpleconsumer.SimpleConsumer`
        :type queued_max_messages: int
        :param fetch_min_bytes: The minimum amount of data (in bytes) that the
            server should return for a fetch request. If insufficient data is
            available, the request will block.
        :type fetch_min_bytes: int
        :param fetch_wait_max_ms: The maximum amount of time (in milliseconds)
            that the server will block before answering a fetch request if
            there isn't sufficient data to immediately satisfy fetch_min_bytes.
        :type fetch_wait_max_ms: int
        :param refresh_leader_backoff_ms: Backoff time (in milliseconds) to
            refresh the leader of a partition after it loses the current leader.
        :type refresh_leader_backoff_ms: int
        :param offsets_channel_backoff_ms: Backoff time to retry failed offset
            commits and fetches.
        :type offsets_channel_backoff_ms: int
        :param offsets_commit_max_retries: The number of times the offset commit
            worker should retry before raising an error.
        :type offsets_commit_max_retries: int
        :param auto_offset_reset: What to do if an offset is out of range. This
            setting indicates how to reset the consumer's internal offset
            counter when an OffsetOutOfRangeError is encountered.
        :type auto_offset_reset: :class:`pykafka.common.OffsetType`
        :param consumer_timeout_ms: Amount of time (in milliseconds) the
            consumer may spend without messages available for consumption
            before raising an error.
        :type consumer_timeout_ms: int
        :param rebalance_max_retries: The number of times the rebalance should
            retry before raising an error.
        :type rebalance_max_retries: int
        :param rebalance_backoff_ms: Backoff time (in milliseconds) between
            retries during rebalance.
        :type rebalance_backoff_ms: int
        :param zookeeper_connection_timeout_ms: The maximum time (in
            milliseconds) that the consumer waits while establishing a
            connection to zookeeper.
        :type zookeeper_connection_timeout_ms: int
        :param zookeeper_connect: Comma-separated (ip1:port1,ip2:port2) strings
            indicating the zookeeper nodes to which to connect.
        :type zookeeper_connect: str
        :param zookeeper: A KazooClient connected to a Zookeeper instance.
            If provided, `zookeeper_connect` is ignored.
        :type zookeeper: :class:`kazoo.client.KazooClient`
        :param auto_start: Whether the consumer should begin communicating
            with zookeeper after __init__ is complete. If false, communication
            can be started with `start()`.
        :type auto_start: bool
        """
        self._cluster = cluster
        self._consumer_group = consumer_group
        self._topic = topic

        self._auto_commit_enable = auto_commit_enable
        self._auto_commit_interval_ms = auto_commit_interval_ms
        self._fetch_message_max_bytes = fetch_message_max_bytes
        self._fetch_min_bytes = fetch_min_bytes
        self._rebalance_max_retries = rebalance_max_retries
        self._num_consumer_fetchers = num_consumer_fetchers
        self._queued_max_messages = queued_max_messages
        self._fetch_wait_max_ms = fetch_wait_max_ms
        self._rebalance_backoff_ms = rebalance_backoff_ms
        self._consumer_timeout_ms = consumer_timeout_ms
        self._offsets_channel_backoff_ms = offsets_channel_backoff_ms
        self._offsets_commit_max_retries = offsets_commit_max_retries
        self._auto_offset_reset = auto_offset_reset
        self._zookeeper_connect = zookeeper_connect
        self._zookeeper_connection_timeout_ms = zookeeper_connection_timeout_ms

        self._consumer = None
        self._consumer_id = "{}:{}".format(socket.gethostname(), uuid4())
        self._partitions = set()
        self._setting_watches = True

        self._topic_path = '/consumers/{}/owners/{}'.format(self._consumer_group,
                                                            self._topic.name)
        self._consumer_id_path = '/consumers/{}/ids'.format(self._consumer_group)

        self._zookeeper = None
        if zookeeper is not None:
            self._zookeeper = zookeeper
        if auto_start is True:
            self.start()

    def __repr__(self):
        return "<{}.{} at {} (consumer_group={})>".format(
            self.__class__.__module__,
            self.__class__.__name__,
            hex(id(self)),
            self._consumer_group
        )

    def start(self):
        """Open connections and join a cluster."""
        if self._zookeeper is None:
            self._setup_zookeeper(self._zookeeper_connect,
                                  self._zookeeper_connection_timeout_ms)
        self._zookeeper.ensure_path(self._topic_path)
        self._add_self()
        self._set_watches()
        self._rebalance()

    def stop(self):
        """Close the zookeeper connection and stop consuming.

        This method should be called as part of a graceful shutdown process.
        """
        self._zookeeper.stop()
        self._consumer.stop()

    def _setup_zookeeper(self, zookeeper_connect, timeout):
        """Open a connection to a ZooKeeper host.

        :param zookeeper_connect: The 'ip:port' address of the zookeeper node to
            which to connect.
        :type zookeeper_connect: str
        :param timeout: Connection timeout (in milliseconds)
        :type timeout: int
        """
        self._zookeeper = KazooClient(zookeeper_connect, timeout=timeout / 1000)
        self._zookeeper.start()

    def _setup_internal_consumer(self):
        """Instantiate an internal SimpleConsumer.

        If there is already a SimpleConsumer instance held by this object,
        disable its workers and mark it for garbage collection before
        creating a new one.
        """
        if self._consumer is not None:
            self._consumer.stop()
        self._consumer = SimpleConsumer(
            self._topic, self._cluster,
            consumer_group=self._consumer_group,
            partitions=list(self._partitions),
            auto_commit_enable=self._auto_commit_enable,
            auto_commit_interval_ms=self._auto_commit_interval_ms,
            fetch_message_max_bytes=self._fetch_message_max_bytes,
            fetch_min_bytes=self._fetch_min_bytes,
            num_consumer_fetchers=self._num_consumer_fetchers,
            queued_max_messages=self._queued_max_messages,
            fetch_wait_max_ms=self._fetch_wait_max_ms,
            consumer_timeout_ms=self._consumer_timeout_ms,
            offsets_channel_backoff_ms=self._offsets_channel_backoff_ms,
            offsets_commit_max_retries=self._offsets_commit_max_retries,
            auto_offset_reset=self._auto_offset_reset
        )

    def _decide_partitions(self, participants):
        """Decide which partitions belong to this consumer.

        Uses the consumer rebalancing algorithm described here
        http://kafka.apache.org/documentation.html

        It is very important that the participants array is sorted,
        since this algorithm runs on each consumer and indexes into the same
        array. The same array index operation must return the same
        result on each consumer.

        :param participants: Sorted list of ids of all other consumers in this
            consumer group.
        :type participants: Iterable of str
        """
        # Freeze and sort partitions so we always have the same results
        p_to_str = lambda p: '-'.join([p.topic.name, str(p.leader.id), str(p.id)])
        all_parts = self._topic.partitions.values()
        all_parts.sort(key=p_to_str)

        # get start point, # of partitions, and remainder
        participants.sort()  # just make sure it's sorted.
        idx = participants.index(self._consumer_id)
        parts_per_consumer = math.floor(len(all_parts) / len(participants))
        remainder_ppc = len(all_parts) % len(participants)

        start = parts_per_consumer * idx + min(idx, remainder_ppc)
        num_parts = parts_per_consumer + (0 if (idx + 1 > remainder_ppc) else 1)

        # assign partitions from i*N to (i+1)*N - 1 to consumer Ci
        new_partitions = itertools.islice(all_parts, start, start + num_parts)
        new_partitions = set(new_partitions)
        log.info(
            'Balancing %i participants for %i partitions. '
            'My Partitions: %s -- Consumers: %s --- All Partitions: %s',
            len(participants), len(all_parts),
            [p_to_str(p) for p in new_partitions],
            str(participants),
            [p_to_str(p) for p in all_parts]
        )
        return new_partitions

    def _get_participants(self):
        """Use zookeeper to get the other consumers of this topic.

        :return: A sorted list of the ids of the other consumers of this
            consumer's topic
        """
        try:
            consumer_ids = self._zookeeper.get_children(self._consumer_id_path)
        except NoNodeException:
            log.debug("Consumer group doesn't exist. "
                      "No participants to find")
            return []

        participants = []
        for id_ in consumer_ids:
            try:
                topic, stat = self._zookeeper.get("%s/%s" % (self._consumer_id_path, id_))
                if topic == self._topic.name:
                    participants.append(id_)
            except NoNodeException:
                pass  # disappeared between ``get_children`` and ``get``
        participants.sort()
        return participants

    def _set_watches(self):
        """Set watches in zookeeper that will trigger rebalances.

        Rebalances should be triggered whenever a broker, topic, or consumer
        znode is changed in zookeeper. This ensures that the balance of the
        consumer group remains up-to-date with the current state of the
        cluster.
        """
        self._setting_watches = True
        # Set all our watches and then rebalance
        broker_path = '/brokers/ids'
        try:
            self._broker_watcher = ChildrenWatch(
                self._zookeeper, broker_path,
                self._brokers_changed
            )
        except NoNodeException:
            raise Exception(
                'The broker_path "%s" does not exist in your '
                'ZooKeeper cluster -- is your Kafka cluster running?'
                % broker_path)

        self._topics_watcher = ChildrenWatch(
            self._zookeeper,
            '/brokers/topics',
            self._topics_changed
        )

        self._consumer_watcher = ChildrenWatch(
            self._zookeeper, self._consumer_id_path,
            self._consumers_changed
        )
        self._setting_watches = False

    def _add_self(self):
        """Register this consumer in zookeeper.

        This method ensures that the number of participants is at most the
        number of partitions.
        """
        participants = self._get_participants()
        if len(self._topic.partitions) <= len(participants):
            raise KafkaException("Cannot add consumer: more consumers than partitions")

        path = '{}/{}'.format(self._consumer_id_path, self._consumer_id)
        self._zookeeper.create(
            path, self._topic.name, ephemeral=True, makepath=True)

    def _rebalance(self):
        """Claim partitions for this consumer.

        This method is called whenever a zookeeper watch is triggered.
        """
        log.info('Rebalancing consumer %s for topic %s.' % (
            self._consumer_id, self._topic.name)
        )

        for i in xrange(self._rebalance_max_retries):
            participants = self._get_participants()
            new_partitions = self._decide_partitions(participants)

            self._remove_partitions(self._partitions - new_partitions)

            try:
                self._add_partitions(new_partitions - self._partitions)
                break
            except NodeExistsError:
                log.debug("Partition still owned")

            log.debug("Retrying")
            time.sleep(i * (self._rebalance_backoff_ms / 1000))

        self._setup_internal_consumer()

    def _path_from_partition(self, p):
        """Given a partition, return its path in zookeeper.

        :type p: :class:`pykafka.partition.Partition`
        """
        return "%s/%s-%s" % (self._topic_path, p.leader.id, p.id)

    def _remove_partitions(self, partitions):
        """Remove partitions from the zookeeper registry for this consumer.

        Also remove these partitions from the consumer's internal
        partition registry.

        :param partitions: The partitions to remove.
        :type partitions: Iterable of :class:`pykafka.partition.Partition`
        """
        for p in partitions:
            assert p in self._partitions
            self._zookeeper.delete(self._path_from_partition(p))
        self._partitions -= partitions

    def _add_partitions(self, partitions):
        """Add partitions to the zookeeper registry for this consumer.

        Also add these partitions to the consumer's internal partition registry.

        :param partitions: The partitions to add.
        :type partitions: Iterable of :class:`pykafka.partition.Partition`
        """
        for p in partitions:
            self._zookeeper.create(
                self._path_from_partition(p), self._consumer_id,
                ephemeral=True
            )
        self._partitions |= partitions

    def _brokers_changed(self, brokers):
        if self._setting_watches:
            return
        log.debug("Rebalance triggered by broker change")
        self._rebalance()

    def _consumers_changed(self, consumers):
        if self._setting_watches:
            return
        log.debug("Rebalance triggered by consumer change")
        self._rebalance()

    def _topics_changed(self, topics):
        if self._setting_watches:
            return
        log.debug("Rebalance triggered by topic change")
        self._rebalance()

    def consume(self, block=True):
        """Get one message from the consumer

        :param block: Whether to block while waiting for a message
        :type block: bool
        """
        return self._consumer.consume(block=block)

    def __iter__(self):
        """Yield an infinite stream of messages from this consumer."""
        while True:
            yield self._consumer.consume()
