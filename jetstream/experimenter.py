import datetime as dt
import logging
import time
from typing import Any, Iterable, List, Mapping, Optional, Union

import attr
import cattr
import pytz
import requests

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True, kw_only=True, slots=True, frozen=True)
class Variant:
    is_control: bool
    slug: str
    ratio: int


def _coerce_none_to_zero(x: Optional[int]) -> int:
    return 0 if x is None else x


@attr.s(auto_attribs=True, kw_only=True, slots=True, frozen=True)
class Branch:
    slug: str
    ratio: int


@attr.s(auto_attribs=True, kw_only=True, slots=True, frozen=True)
class Experiment:
    """
    Common Experimenter experiment representation.
    Attributes:
        experimenter_slug: Slug generated by Experimenter for V1 experiments;
            None for V6 experiments
        normandy_slug: V1 experiment normandy_slug; V6 experiment slug
        type: V1 experiment type; always "v6" for V6 experiments
        status: V1 experiment status; "Live" for active V6 experiments,
            "Complete" for V6 experiments with endDate in the past
        branches: V1 experiment variants converted to branches; V6 experiment branches
        probe_sets: List of labels for probe_sets
        start_date: experiment start_date
        end_date: experiment end_date
        proposed_enrollment: experiment proposed_enrollment
        reference_branch: V1 experiment branch slug where is_control is True;
            V6 experiment reference_branch
    """

    experimenter_slug: Optional[str]
    normandy_slug: Optional[str]
    type: str
    status: Optional[str]
    branches: List[Branch]
    probe_sets: List[str]
    start_date: Optional[dt.datetime]
    end_date: Optional[dt.datetime]
    proposed_enrollment: Optional[int]
    reference_branch: Optional[str]
    is_high_population: bool


@attr.s(auto_attribs=True, kw_only=True, slots=True, frozen=True)
class ExperimentV1:
    """Experimenter v1 experiment."""

    slug: str  # experimenter slug
    type: str
    status: str
    start_date: Optional[dt.datetime]
    end_date: Optional[dt.datetime]
    proposed_enrollment: Optional[int] = attr.ib(converter=_coerce_none_to_zero)
    variants: List[Variant]
    normandy_slug: Optional[str] = None
    is_high_population: Optional[bool] = None

    @staticmethod
    def _unix_millis_to_datetime(num: Optional[float]) -> Optional[dt.datetime]:
        if num is None:
            return None
        return dt.datetime.fromtimestamp(num / 1e3, pytz.utc)

    @classmethod
    def from_dict(cls, d) -> "ExperimentV1":
        converter = cattr.Converter()
        converter.register_structure_hook(
            dt.datetime,
            lambda num, _: cls._unix_millis_to_datetime(num),
        )
        return converter.structure(d, cls)

    def to_experiment(self) -> "Experiment":
        """Convert to Experiment."""
        branches = [Branch(slug=variant.slug, ratio=variant.ratio) for variant in self.variants]
        control_slug = None

        control_slugs = [variant.slug for variant in self.variants if variant.is_control]
        if len(control_slugs) == 1:
            control_slug = control_slugs[0]

        return Experiment(
            normandy_slug=self.normandy_slug,
            experimenter_slug=self.slug,
            type=self.type,
            status=self.status,
            start_date=self.start_date,
            end_date=self.end_date,
            proposed_enrollment=self.proposed_enrollment,
            branches=branches,
            probe_sets=[],
            reference_branch=control_slug,
            is_high_population=self.is_high_population or False,
        )


@attr.s(auto_attribs=True, kw_only=True, slots=True, frozen=True)
class ExperimentV6:
    """Represents a v6 experiment from Experimenter."""

    slug: str  # Normandy slug
    branches: List[Branch]
    startDate: Optional[dt.datetime]
    endDate: Optional[dt.datetime]
    proposedEnrollment: int
    referenceBranch: Optional[str]
    probeSets: List[str]

    @classmethod
    def from_dict(cls, d) -> "ExperimentV6":
        converter = cattr.Converter()
        converter.register_structure_hook(
            dt.datetime,
            lambda num, _: dt.datetime.fromisoformat(num.replace("Z", "+00:00")),
        )
        return converter.structure(d, cls)

    def to_experiment(self) -> "Experiment":
        """Convert to Experiment."""
        return Experiment(
            normandy_slug=self.slug,
            experimenter_slug=None,
            type="v6",
            status="Live"
            if (self.endDate and self.endDate >= pytz.utc.localize(dt.datetime.now()))
            or self.endDate is None
            else "Complete",
            start_date=self.startDate,
            end_date=self.endDate,
            proposed_enrollment=self.proposedEnrollment,
            branches=self.branches,
            probe_sets=self.probeSets,
            reference_branch=self.referenceBranch,
            is_high_population=False,
        )


@attr.s(auto_attribs=True)
class ExperimentCollection:
    experiments: List[Experiment] = attr.Factory(list)

    MAX_RETRIES = 3
    EXPERIMENTER_API_URL_V1 = "https://experimenter.services.mozilla.com/api/v1/experiments/"

    # for nimbus experiments
    EXPERIMENTER_API_URL_V6 = "https://experimenter.services.mozilla.com/api/v6/experiments/"

    @classmethod
    def from_experimenter(cls, session: requests.Session = None) -> "ExperimentCollection":
        session = session or requests.Session()

        legacy_experiments_json = cls._retry_get(session, cls.EXPERIMENTER_API_URL_V1)
        legacy_experiments = []

        for experiment in legacy_experiments_json:
            if experiment["type"] != "rapid":
                try:
                    legacy_experiments.append(ExperimentV1.from_dict(experiment).to_experiment())
                except Exception as e:
                    logger.exception(str(e), exc_info=e, extra={"experiment": experiment["slug"]})

        nimbus_experiments_json = cls._retry_get(session, cls.EXPERIMENTER_API_URL_V6)
        nimbus_experiments = []

        for experiment in nimbus_experiments_json:
            try:
                nimbus_experiments.append(ExperimentV6.from_dict(experiment).to_experiment())
            except Exception as e:
                logger.exception(str(e), exc_info=e, extra={"experiment": experiment["slug"]})

        return cls(nimbus_experiments + legacy_experiments)

    @staticmethod
    def _retry_get(session, url) -> Iterable[Mapping[str, Any]]:
        for _i in range(ExperimentCollection.MAX_RETRIES):
            try:
                blob = session.get(url).json()
                break
            except Exception:
                logger.info(f"Error fetching from {url}. Retrying...")
                time.sleep(1)
        else:
            raise Exception(f"Too many retries for {url}")
        return blob

    def of_type(self, type_or_types: Union[str, Iterable[str]]) -> "ExperimentCollection":
        if isinstance(type_or_types, str):
            type_or_types = (type_or_types,)
        cls = type(self)
        return cls([ex for ex in self.experiments if ex.type in type_or_types])

    def ever_launched(self) -> "ExperimentCollection":
        cls = type(self)
        return cls(
            [
                ex
                for ex in self.experiments
                if ex.status in ("Complete", "Live") or ex.status is None
            ]
        )

    def with_slug(self, slug: str) -> "ExperimentCollection":
        cls = type(self)
        return cls(
            [
                ex
                for ex in self.experiments
                if ex.experimenter_slug == slug or ex.normandy_slug == slug
            ]
        )

    def started_since(self, since: dt.datetime) -> "ExperimentCollection":
        """All experiments that ever launched after a given time.

        since should be a tz-aware datetime."""
        cls = type(self)
        return cls(
            [
                ex
                for ex in self.ever_launched().experiments
                if ex.start_date and ex.start_date >= since
            ]
        )

    def end_on_or_after(self, after: dt.datetime) -> "ExperimentCollection":
        """All experiments that ever launched that end on or after the specified time.

        after should be a tz-aware datetime."""
        cls = type(self)
        # V6 experiments might not have an endDate set yet
        return cls(
            [
                ex
                for ex in self.ever_launched().experiments
                if (ex.end_date and ex.end_date >= after) or ex.end_date is None
            ]
        )
