# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from abc import ABC, abstractmethod
from random import randrange
from typing import Any, Callable, Optional, Sequence, TypeAlias, Union

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.span import INVALID_SPAN
from opentelemetry.util.types import Attributes

from .exemplar import Exemplar


class ExemplarReservoir(ABC):
    """ExemplarReservoir provide a method to offer measurements to the reservoir
    and another to collect accumulated Exemplars.

    Note:
        The constructor MUST accept ``**kwargs`` that may be set from aggregation
        parameters.

    Reference:
        https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/metrics/sdk.md#exemplarreservoir
    """

    @abstractmethod
    def offer(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> None:
        """Offers a measurement to be sampled."""
        raise NotImplementedError("ExemplarReservoir.offer is not implemented")

    @abstractmethod
    def collect(self, point_attributes: Attributes) -> list[Exemplar]:
        """Returns accumulated Exemplars and also resets the reservoir for the next
        sampling period

        Args:
            point_attributes The attributes associated with metric point.

        Returns:
            a list of :class:`opentelemetry.sdk.metrics.exemplar.Exemplar`s. Returned
            exemplars contain the attributes that were filtered out by the aggregator,
            but recorded alongside the original measurement.
        """
        raise NotImplementedError(
            "ExemplarReservoir.collect is not implemented"
        )


class ExemplarBucket:
    def __init__(self) -> None:
        self.__value: Union[int, float] = 0
        self.__attributes: Attributes = None
        self.__time_unix_nano: int = 0
        self.__span_id: Optional[str] = None
        self.__trace_id: Optional[str] = None
        self.__offered: bool = False

    def offer(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> None:
        """Offers a measurement to be sampled."""
        self.__value = value
        self.__time_unix_nano = time_unix_nano
        self.__attributes = attributes
        span = trace.get_current_span(ctx)
        if span != INVALID_SPAN:
            span_context = span.get_span_context()
            self.__span_id = span_context.span_id
            self.__trace_id = span_context.trace_id

        self.__offered = True

    def collect(self, point_attributes: Attributes) -> Exemplar | None:
        """May return an Exemplar and resets the bucket for the next sampling period."""
        if not self.__offered:
            return None

        filtered_attributes = (
            {
                k: v
                for k, v in self.__attributes.items()
                if k not in point_attributes
            }
            if self.__attributes
            else None
        )

        exemplar = Exemplar(
            filtered_attributes,
            self.__value,
            self.__time_unix_nano,
            self.__span_id,
            self.__trace_id,
        )
        self.__reset()
        return exemplar

    def __reset(self) -> None:
        self.__value = 0
        self.__attributes = {}
        self.__time_unix_nano = 0
        self.__span_id = None
        self.__trace_id = None
        self.__offered = False


class FixedSizeExemplarReservoirABC(ExemplarReservoir):
    """Abstract class for a reservoir with fixed size."""

    def __init__(self, size: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._size: int = size
        self._reservoir_storage: list[ExemplarBucket] = [
            ExemplarBucket() for _ in range(self._size)
        ]

    def maxSize(self) -> int:
        """Reservoir maximal size"""
        return self._size

    def collect(self, point_attributes: Attributes) -> list[Exemplar]:
        """Returns accumulated Exemplars and also resets the reservoir for the next
        sampling period

        Args:
            point_attributes The attributes associated with metric point.

        Returns:
            a list of :class:`opentelemetry.sdk.metrics.exemplar.Exemplar`s. Returned
            exemplars contain the attributes that were filtered out by the aggregator,
            but recorded alongside the original measurement.
        """
        exemplars = filter(
            lambda e: e is not None,
            map(
                lambda bucket: bucket.collect(point_attributes),
                self._reservoir_storage,
            ),
        )
        self._reset()
        return [*exemplars]

    def _reset(self) -> None:
        """Reset the reservoir."""
        pass


class SimpleFixedSizeExemplarReservoir(FixedSizeExemplarReservoirABC):
    """This reservoir uses an uniformly-weighted sampling algorithm based on the number
    of samples the reservoir has seen so far to determine if the offered measurements
    should be sampled.

    Reference:
        https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/metrics/sdk.md#simplefixedsizeexemplarreservoir
    """

    def __init__(self, size: int = 1, **kwargs) -> None:
        super().__init__(size, **kwargs)
        self._measurements_seen: int = 0

    def _reset(self) -> None:
        super()._reset()
        self._measurements_seen = 0

    def offer(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> None:
        """Offers a measurement to be sampled."""
        index = self._find_bucket_index(value, time_unix_nano, attributes, ctx)
        if index != -1:
            self._reservoir_storage[index].offer(
                value, time_unix_nano, attributes, ctx
            )

    def _find_bucket_index(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> int:
        if self._measurements_seen < self._size:
            return self._measurements_seen

        index = randrange(0, self._measurements_seen)
        self._measurements_seen += 1
        return index if index < self._size else -1


class AlignedHistogramBucketExemplarReservoir(FixedSizeExemplarReservoirABC):
    """This Exemplar reservoir takes a configuration parameter that is the
    configuration of a Histogram. This implementation keeps the last seen measurement
    that falls within a histogram bucket.

    Reference:
        https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/metrics/sdk.md#alignedhistogrambucketexemplarreservoir
    """

    def __init__(self, boundaries: Sequence[float], **kwargs) -> None:
        super().__init__(len(boundaries) + 1, **kwargs)
        self._boundaries: Sequence[float] = boundaries

    def offer(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> None:
        """Offers a measurement to be sampled."""
        index = self._find_bucket_index(value, time_unix_nano, attributes, ctx)
        self._reservoir_storage[index].offer(
            value, time_unix_nano, attributes, ctx
        )

    def _find_bucket_index(
        self,
        value: Union[int, float],
        time_unix_nano: int,
        attributes: Attributes,
        ctx: Context,
    ) -> int:
        for i, boundary in enumerate(self._boundaries):
            if value <= boundary:
                return i
        return len(self._boundaries)


ExemplarReservoirFactory: TypeAlias = Callable[
    [dict[str, Any]], ExemplarReservoir
]
ExemplarReservoirFactory.__doc__ = """ExemplarReservoir factory.

It may receive the Aggregation parameters it is bounded to; e.g.
the _ExplicitBucketHistogramAggregation will provide the boundaries.
"""
