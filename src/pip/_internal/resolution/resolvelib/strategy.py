import abc

from pip._vendor.six import add_metaclass


@add_metaclass(abc.ABCMeta)
class AbstractStrategy:

    @abc.abstractmethod
    def get_preferred_candidate(
            self,
            resolution,  # type: Optional[Candidate]
            candidates,  # type: Sequence[Candidate]
            information,  # type: Sequence[Tuple[Requirement, Candidate]]
    ):
        # type: (...) -> int
        raise NotImplementedError()

    @abc.abstractmethod
    def sort_candidates(self, candidates):
        # type: (Sequence[Candidates]) -> Sequence[Candidate]
        raise NotImplementedError()


class DefaultStrategy(AbstractStrategy):

    def get_preferred_candidate(
            self,
            resolution,  # type: Optional[Candidate]
            candidates,  # type: Sequence[Candidate]
            information,  # type: Sequence[Tuple[Requirement, Candidate]]
    ):
        # type: (...) -> int
        return len(candidates)

    def sort_candidates(self, candidates):
        # type: (Sequence[Candidate]) -> Sequence[Candidate]
        return reversed(candidates)


class EarliestCompatible(AbstractStrategy):

    def get_preferred_candidate(
            self,
            resolution,  # type: Optional[Candidate]
            candidates,  # type: Sequence[Candidate]
            information,  # type: Sequence[Tuple[Requirement, Candidate]]
    ):
        # type: (...) -> int
        return 0

    def sort_candidates(self, candidates):
        # type: (Sequence[Candidate]) -> Sequence[Candidate]
        return candidates


def strategy_factory(name):
    # type: (str) -> AbstractStrategy
    f = {
        'earliest-compatible': EarliestCompatible,
    }.get(name, DefaultStrategy)
    return f()
