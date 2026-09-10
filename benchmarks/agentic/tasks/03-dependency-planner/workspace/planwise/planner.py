class PlanError(ValueError):
    pass


def parse_tasks(document):
    raise NotImplementedError


def plan(tasks, jobs=1):
    raise NotImplementedError


def to_dot(tasks):
    raise NotImplementedError
