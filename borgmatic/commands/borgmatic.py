import collections
import json
import logging
import os
import sys
from subprocess import CalledProcessError

import colorama
import pkg_resources

from borgmatic.borg import check as borg_check
from borgmatic.borg import create as borg_create
from borgmatic.borg import environment as borg_environment
from borgmatic.borg import extract as borg_extract
from borgmatic.borg import info as borg_info
from borgmatic.borg import init as borg_init
from borgmatic.borg import list as borg_list
from borgmatic.borg import mount as borg_mount
from borgmatic.borg import prune as borg_prune
from borgmatic.borg import umount as borg_umount
from borgmatic.commands.arguments import parse_arguments
from borgmatic.config import checks, collect, convert, validate
from borgmatic.hooks import command, dispatch, dump, monitor
from borgmatic.logger import configure_logging, should_do_markup
from borgmatic.signals import configure_signals
from borgmatic.verbosity import verbosity_to_log_level

logger = logging.getLogger(__name__)

LEGACY_CONFIG_PATH = '/etc/borgmatic/config'


def run_configuration(config_filename, config, arguments):
    '''
    Given a config filename, the corresponding parsed config dict, and command-line arguments as a
    dict from subparser name to a namespace of parsed arguments, execute its defined pruning,
    backups, consistency checks, and/or other actions.

    Yield a combination of:

      * JSON output strings from successfully executing any actions that produce JSON
      * logging.LogRecord instances containing errors from any actions or backup hooks that fail
    '''
    (location, storage, retention, consistency, hooks) = (
        config.get(section_name, {})
        for section_name in ('location', 'storage', 'retention', 'consistency', 'hooks')
    )
    global_arguments = arguments['global']

    local_path = location.get('local_path', 'borg')
    remote_path = location.get('remote_path')
    borg_environment.initialize(storage)
    encountered_error = None
    error_repository = ''

    try:
        if {'prune', 'create', 'check'}.intersection(arguments):
            dispatch.call_hooks(
                'ping_monitor',
                hooks,
                config_filename,
                monitor.MONITOR_HOOK_NAMES,
                monitor.State.START,
                global_arguments.dry_run,
            )
        if 'create' in arguments:
            command.execute_hook(
                hooks.get('before_backup'),
                hooks.get('umask'),
                config_filename,
                'pre-backup',
                global_arguments.dry_run,
            )
            dispatch.call_hooks(
                'dump_databases',
                hooks,
                config_filename,
                dump.DATABASE_HOOK_NAMES,
                location,
                global_arguments.dry_run,
            )
    except (OSError, CalledProcessError) as error:
        encountered_error = error
        yield from make_error_log_records(
            '{}: Error running pre-backup hook'.format(config_filename), error
        )

    if not encountered_error:
        for repository_path in location['repositories']:
            try:
                yield from run_actions(
                    arguments=arguments,
                    location=location,
                    storage=storage,
                    retention=retention,
                    consistency=consistency,
                    hooks=hooks,
                    local_path=local_path,
                    remote_path=remote_path,
                    repository_path=repository_path,
                )
            except (OSError, CalledProcessError, ValueError) as error:
                encountered_error = error
                error_repository = repository_path
                yield from make_error_log_records(
                    '{}: Error running actions for repository'.format(repository_path), error
                )

    if not encountered_error:
        try:
            if 'create' in arguments:
                dispatch.call_hooks(
                    'remove_database_dumps',
                    hooks,
                    config_filename,
                    dump.DATABASE_HOOK_NAMES,
                    location,
                    global_arguments.dry_run,
                )
                command.execute_hook(
                    hooks.get('after_backup'),
                    hooks.get('umask'),
                    config_filename,
                    'post-backup',
                    global_arguments.dry_run,
                )
            if {'prune', 'create', 'check'}.intersection(arguments):
                dispatch.call_hooks(
                    'ping_monitor',
                    hooks,
                    config_filename,
                    monitor.MONITOR_HOOK_NAMES,
                    monitor.State.FINISH,
                    global_arguments.dry_run,
                )
        except (OSError, CalledProcessError) as error:
            encountered_error = error
            yield from make_error_log_records(
                '{}: Error running post-backup hook'.format(config_filename), error
            )

    if encountered_error:
        try:
            command.execute_hook(
                hooks.get('on_error'),
                hooks.get('umask'),
                config_filename,
                'on-error',
                global_arguments.dry_run,
                repository=error_repository,
                error=encountered_error,
                output=getattr(encountered_error, 'output', ''),
            )
            dispatch.call_hooks(
                'ping_monitor',
                hooks,
                config_filename,
                monitor.MONITOR_HOOK_NAMES,
                monitor.State.FAIL,
                global_arguments.dry_run,
            )
        except (OSError, CalledProcessError) as error:
            yield from make_error_log_records(
                '{}: Error running on-error hook'.format(config_filename), error
            )


def run_actions(
    *,
    arguments,
    location,
    storage,
    retention,
    consistency,
    hooks,
    local_path,
    remote_path,
    repository_path
):  # pragma: no cover
    '''
    Given parsed command-line arguments as an argparse.ArgumentParser instance, several different
    configuration dicts, local and remote paths to Borg, and a repository name, run all actions
    from the command-line arguments on the given repository.

    Yield JSON output strings from executing any actions that produce JSON.

    Raise OSError or subprocess.CalledProcessError if an error occurs running a command for an
    action. Raise ValueError if the arguments or configuration passed to action are invalid.
    '''
    repository = os.path.expanduser(repository_path)
    global_arguments = arguments['global']
    dry_run_label = ' (dry run; not making any changes)' if global_arguments.dry_run else ''
    if 'init' in arguments:
        logger.info('{}: Initializing repository'.format(repository))
        borg_init.initialize_repository(
            repository,
            storage,
            arguments['init'].encryption_mode,
            arguments['init'].append_only,
            arguments['init'].storage_quota,
            local_path=local_path,
            remote_path=remote_path,
        )
    if 'prune' in arguments:
        logger.info('{}: Pruning archives{}'.format(repository, dry_run_label))
        borg_prune.prune_archives(
            global_arguments.dry_run,
            repository,
            storage,
            retention,
            local_path=local_path,
            remote_path=remote_path,
            stats=arguments['prune'].stats,
        )
    if 'create' in arguments:
        logger.info('{}: Creating archive{}'.format(repository, dry_run_label))
        json_output = borg_create.create_archive(
            global_arguments.dry_run,
            repository,
            location,
            storage,
            local_path=local_path,
            remote_path=remote_path,
            progress=arguments['create'].progress,
            stats=arguments['create'].stats,
            json=arguments['create'].json,
        )
        if json_output:
            yield json.loads(json_output)
    if 'check' in arguments and checks.repository_enabled_for_checks(repository, consistency):
        logger.info('{}: Running consistency checks'.format(repository))
        borg_check.check_archives(
            repository,
            storage,
            consistency,
            local_path=local_path,
            remote_path=remote_path,
            repair=arguments['check'].repair,
            only_checks=arguments['check'].only,
        )
    if 'extract' in arguments:
        if arguments['extract'].repository is None or validate.repositories_match(
            repository, arguments['extract'].repository
        ):
            logger.info(
                '{}: Extracting archive {}'.format(repository, arguments['extract'].archive)
            )
            borg_extract.extract_archive(
                global_arguments.dry_run,
                repository,
                arguments['extract'].archive,
                arguments['extract'].paths,
                location,
                storage,
                local_path=local_path,
                remote_path=remote_path,
                destination_path=arguments['extract'].destination,
                progress=arguments['extract'].progress,
            )
    if 'mount' in arguments:
        if arguments['mount'].repository is None or validate.repositories_match(
            repository, arguments['mount'].repository
        ):
            if arguments['mount'].archive:
                logger.info(
                    '{}: Mounting archive {}'.format(repository, arguments['mount'].archive)
                )
            else:
                logger.info('{}: Mounting repository'.format(repository))

            borg_mount.mount_archive(
                repository,
                arguments['mount'].archive,
                arguments['mount'].mount_point,
                arguments['mount'].paths,
                arguments['mount'].foreground,
                arguments['mount'].options,
                storage,
                local_path=local_path,
                remote_path=remote_path,
            )
    if 'restore' in arguments:
        if arguments['restore'].repository is None or validate.repositories_match(
            repository, arguments['restore'].repository
        ):
            logger.info(
                '{}: Restoring databases from archive {}'.format(
                    repository, arguments['restore'].archive
                )
            )

            restore_names = arguments['restore'].databases or []
            if 'all' in restore_names:
                restore_names = []

            # Extract dumps for the named databases from the archive.
            dump_patterns = dispatch.call_hooks(
                'make_database_dump_patterns',
                hooks,
                repository,
                dump.DATABASE_HOOK_NAMES,
                location,
                restore_names,
            )

            borg_extract.extract_archive(
                global_arguments.dry_run,
                repository,
                arguments['restore'].archive,
                dump.convert_glob_patterns_to_borg_patterns(
                    dump.flatten_dump_patterns(dump_patterns, restore_names)
                ),
                location,
                storage,
                local_path=local_path,
                remote_path=remote_path,
                destination_path='/',
                progress=arguments['restore'].progress,
                # We don't want glob patterns that don't match to error.
                error_on_warnings=False,
            )

            # Map the restore names or detected dumps to the corresponding database configurations.
            restore_databases = dump.get_per_hook_database_configurations(
                hooks, restore_names, dump_patterns
            )

            # Finally, restore the databases and cleanup the dumps.
            dispatch.call_hooks(
                'restore_database_dumps',
                restore_databases,
                repository,
                dump.DATABASE_HOOK_NAMES,
                location,
                global_arguments.dry_run,
            )
            dispatch.call_hooks(
                'remove_database_dumps',
                restore_databases,
                repository,
                dump.DATABASE_HOOK_NAMES,
                location,
                global_arguments.dry_run,
            )
    if 'list' in arguments:
        if arguments['list'].repository is None or validate.repositories_match(
            repository, arguments['list'].repository
        ):
            logger.info('{}: Listing archives'.format(repository))
            json_output = borg_list.list_archives(
                repository,
                storage,
                list_arguments=arguments['list'],
                local_path=local_path,
                remote_path=remote_path,
            )
            if json_output:
                yield json.loads(json_output)
    if 'info' in arguments:
        if arguments['info'].repository is None or validate.repositories_match(
            repository, arguments['info'].repository
        ):
            logger.info('{}: Displaying summary info for archives'.format(repository))
            json_output = borg_info.display_archives_info(
                repository,
                storage,
                info_arguments=arguments['info'],
                local_path=local_path,
                remote_path=remote_path,
            )
            if json_output:
                yield json.loads(json_output)


def load_configurations(config_filenames):
    '''
    Given a sequence of configuration filenames, load and validate each configuration file. Return
    the results as a tuple of: dict of configuration filename to corresponding parsed configuration,
    and sequence of logging.LogRecord instances containing any parse errors.
    '''
    # Dict mapping from config filename to corresponding parsed config dict.
    configs = collections.OrderedDict()
    logs = []

    # Parse and load each configuration file.
    for config_filename in config_filenames:
        try:
            configs[config_filename] = validate.parse_configuration(
                config_filename, validate.schema_filename()
            )
        except (ValueError, OSError, validate.Validation_error) as error:
            logs.extend(
                [
                    logging.makeLogRecord(
                        dict(
                            levelno=logging.CRITICAL,
                            levelname='CRITICAL',
                            msg='{}: Error parsing configuration file'.format(config_filename),
                        )
                    ),
                    logging.makeLogRecord(
                        dict(levelno=logging.CRITICAL, levelname='CRITICAL', msg=error)
                    ),
                ]
            )

    return (configs, logs)


def log_record(suppress_log=False, **kwargs):
    '''
    Create a log record based on the given makeLogRecord() arguments, one of which must be
    named "levelno". Log the record (unless suppress log is set) and return it.
    '''
    record = logging.makeLogRecord(kwargs)
    if suppress_log:
        return record

    logger.handle(record)
    return record


def make_error_log_records(message, error=None):
    '''
    Given error message text and an optional exception object, yield a series of logging.LogRecord
    instances with error summary information. As a side effect, log each record.
    '''
    if not error:
        yield log_record(levelno=logging.CRITICAL, levelname='CRITICAL', msg=message)
        return

    try:
        raise error
    except CalledProcessError as error:
        yield log_record(levelno=logging.CRITICAL, levelname='CRITICAL', msg=message)
        if error.output:
            # Suppress these logs for now and save full error output for the log summary at the end.
            yield log_record(
                levelno=logging.CRITICAL, levelname='CRITICAL', msg=error.output, suppress_log=True
            )
        yield log_record(levelno=logging.CRITICAL, levelname='CRITICAL', msg=error)
    except (ValueError, OSError) as error:
        yield log_record(levelno=logging.CRITICAL, levelname='CRITICAL', msg=message)
        yield log_record(levelno=logging.CRITICAL, levelname='CRITICAL', msg=error)
    except:  # noqa: E722
        # Raising above only as a means of determining the error type. Swallow the exception here
        # because we don't want the exception to propagate out of this function.
        pass


def get_local_path(configs):
    '''
    Arbitrarily return the local path from the first configuration dict. Default to "borg" if not
    set.
    '''
    return next(iter(configs.values())).get('location', {}).get('local_path', 'borg')


def collect_configuration_run_summary_logs(configs, arguments):
    '''
    Given a dict of configuration filename to corresponding parsed configuration, and parsed
    command-line arguments as a dict from subparser name to a parsed namespace of arguments, run
    each configuration file and yield a series of logging.LogRecord instances containing summary
    information about each run.

    As a side effect of running through these configuration files, output their JSON results, if
    any, to stdout.
    '''
    # Run cross-file validation checks.
    if 'extract' in arguments:
        repository = arguments['extract'].repository
    elif 'list' in arguments and arguments['list'].archive:
        repository = arguments['list'].repository
    elif 'mount' in arguments:
        repository = arguments['mount'].repository
    else:
        repository = None

    if repository:
        try:
            validate.guard_configuration_contains_repository(repository, configs)
        except ValueError as error:
            yield from make_error_log_records(str(error))
            return

    if not configs:
        yield from make_error_log_records(
            '{}: No configuration files found'.format(' '.join(arguments['global'].config_paths))
        )
        return

    if 'create' in arguments:
        try:
            for config_filename, config in configs.items():
                hooks = config.get('hooks', {})
                command.execute_hook(
                    hooks.get('before_everything'),
                    hooks.get('umask'),
                    config_filename,
                    'pre-everything',
                    arguments['global'].dry_run,
                )
        except (CalledProcessError, ValueError, OSError) as error:
            yield from make_error_log_records('Error running pre-everything hook', error)
            return

    # Execute the actions corresponding to each configuration file.
    json_results = []
    for config_filename, config in configs.items():
        results = list(run_configuration(config_filename, config, arguments))
        error_logs = tuple(result for result in results if isinstance(result, logging.LogRecord))

        if error_logs:
            yield from make_error_log_records(
                '{}: Error running configuration file'.format(config_filename)
            )
            yield from error_logs
        else:
            yield logging.makeLogRecord(
                dict(
                    levelno=logging.INFO,
                    levelname='INFO',
                    msg='{}: Successfully ran configuration file'.format(config_filename),
                )
            )
            if results:
                json_results.extend(results)

    if 'umount' in arguments:
        logger.info('Unmounting mount point {}'.format(arguments['umount'].mount_point))
        try:
            borg_umount.unmount_archive(
                mount_point=arguments['umount'].mount_point, local_path=get_local_path(configs)
            )
        except (CalledProcessError, OSError) as error:
            yield from make_error_log_records('Error unmounting mount point', error)

    if json_results:
        sys.stdout.write(json.dumps(json_results))

    if 'create' in arguments:
        try:
            for config_filename, config in configs.items():
                hooks = config.get('hooks', {})
                command.execute_hook(
                    hooks.get('after_everything'),
                    hooks.get('umask'),
                    config_filename,
                    'post-everything',
                    arguments['global'].dry_run,
                )
        except (CalledProcessError, ValueError, OSError) as error:
            yield from make_error_log_records('Error running post-everything hook', error)


def exit_with_help_link():  # pragma: no cover
    '''
    Display a link to get help and exit with an error code.
    '''
    logger.critical('')
    logger.critical('Need some help? https://torsion.org/borgmatic/#issues')
    sys.exit(1)


def main():  # pragma: no cover
    configure_signals()

    try:
        arguments = parse_arguments(*sys.argv[1:])
    except ValueError as error:
        configure_logging(logging.CRITICAL)
        logger.critical(error)
        exit_with_help_link()
    except SystemExit as error:
        if error.code == 0:
            raise error
        configure_logging(logging.CRITICAL)
        logger.critical('Error parsing arguments: {}'.format(' '.join(sys.argv)))
        exit_with_help_link()

    global_arguments = arguments['global']
    if global_arguments.version:
        print(pkg_resources.require('borgmatic')[0].version)
        sys.exit(0)

    config_filenames = tuple(collect.collect_config_filenames(global_arguments.config_paths))
    configs, parse_logs = load_configurations(config_filenames)

    colorama.init(autoreset=True, strip=not should_do_markup(global_arguments.no_color, configs))
    try:
        configure_logging(
            verbosity_to_log_level(global_arguments.verbosity),
            verbosity_to_log_level(global_arguments.syslog_verbosity),
            verbosity_to_log_level(global_arguments.log_file_verbosity),
            global_arguments.log_file,
        )
    except (FileNotFoundError, PermissionError) as error:
        configure_logging(logging.CRITICAL)
        logger.critical('Error configuring logging: {}'.format(error))
        exit_with_help_link()

    logger.debug('Ensuring legacy configuration is upgraded')
    convert.guard_configuration_upgraded(LEGACY_CONFIG_PATH, config_filenames)

    summary_logs = parse_logs + list(collect_configuration_run_summary_logs(configs, arguments))
    summary_logs_max_level = max(log.levelno for log in summary_logs)

    for message in ('', 'summary:'):
        log_record(
            levelno=summary_logs_max_level,
            levelname=logging.getLevelName(summary_logs_max_level),
            msg=message,
        )

    for log in summary_logs:
        logger.handle(log)

    if summary_logs_max_level >= logging.CRITICAL:
        exit_with_help_link()
