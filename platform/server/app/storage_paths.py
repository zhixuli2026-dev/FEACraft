"""Reserved local paths shared by file staging and case readers."""

FILE_STAGE = '.vibeflow-file-staging'


def is_file_stage(name):
    return name.rstrip(' .').casefold() == FILE_STAGE


def contains_file_stage(path):
    return any(is_file_stage(part) for part in path.replace('\\', '/').split('/'))
