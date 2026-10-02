# Written by Steve Baskauf 2020-06-29 CC0
# Updated to run as a stand-alone script 2021-07-26
# Additional modifications to require less manual work 2023-08-27

import csv
import json
import yaml
import re
import sys
import os
import shutil
import datetime
import copy
import hashlib
import tempfile
import urllib.parse
import subprocess
from pathlib import Path
import pandas as pd

from document_metadata_processing.tdwg_docs_metadata_update import update_document_metadata

# -----------------------
# Transaction wrapper
# -----------------------
# The processor historically writes many repository files incrementally. To make
# a failed run atomic without changing those established generation semantics,
# the complete workflow is first executed in a temporary copy of the repository.
# Only a successful staged run is allowed to publish its file delta back to the
# caller's working tree.
_TRANSACTION_ENV = 'TDWG_PROCESS_TRANSACTION_CHILD'


def _transaction_ignored(relative_path):
    """Return True for repository content that is outside the metadata transaction."""
    parts = Path(relative_path).parts
    if not parts:
        return False
    if parts[0] == '.git' or '__pycache__' in parts:
        return True
    # Processing logs are audit output, not repository metadata. They are
    # excluded from the metadata manifest and published separately after a
    # successful staged run.
    if (len(parts) >= 2 and parts[0] == 'process' and
            parts[1] in {'logs', 'reports'}):
        return True
    return False


def _tree_manifest(root):
    """Return {relative POSIX path: sha256} for transactional repository files."""
    root = Path(root)
    manifest = {}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _transaction_ignored(rel):
            continue
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        manifest[rel.as_posix()] = digest.hexdigest()
    return manifest


def _copy_repository_for_transaction(source_root, staged_root):
    """Create the candidate workspace without Git internals, caches, logs, or reports."""
    source_root = Path(source_root)
    staged_root = Path(staged_root)

    def ignore(directory, names):
        rel_dir = Path(directory).resolve().relative_to(source_root.resolve())
        ignored = set()
        for name in names:
            rel = rel_dir / name
            if _transaction_ignored(rel):
                ignored.add(name)
        return ignored

    shutil.copytree(source_root, staged_root, ignore=ignore)


def _file_sha256(path):
    """Return the SHA-256 digest for one file."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_transaction_delta(source_root, staged_root, before, after):
    """Publish the staged delta as one rollback-capable repository transaction."""
    source_root = Path(source_root)
    staged_root = Path(staged_root)

    deleted = sorted(set(before) - set(after))
    changed = sorted(
        path for path in after
        if path not in before or before[path] != after[path]
    )
    affected = sorted(set(changed) | set(deleted))

    # Refuse to overwrite concurrent edits made in the real working tree while
    # the child was processing. Only paths that the staged run intends to change
    # need to be checked.
    conflicts = []
    for rel_string in affected:
        destination = source_root / rel_string
        expected_digest = before.get(rel_string)
        if expected_digest is None:
            if destination.exists():
                conflicts.append(rel_string + ' (created during processing)')
        elif not destination.is_file():
            conflicts.append(rel_string + ' (removed or replaced during processing)')
        elif _file_sha256(destination) != expected_digest:
            conflicts.append(rel_string + ' (modified during processing)')

    if conflicts:
        details = '\n  - '.join(conflicts)
        raise RuntimeError(
            'Repository changed while processing; staged results were not published:\n'
            '  - ' + details
        )

    # Preserve the exact pre-publication state of every affected file outside
    # the repository. If any later publication operation fails, all affected
    # paths can therefore be restored to this state.
    with tempfile.TemporaryDirectory(prefix='tdwg-process-rollback-') as rollback_dir:
        rollback_root = Path(rollback_dir)
        existed_before_publish = set()
        for rel_string in affected:
            destination = source_root / rel_string
            if destination.is_file():
                existed_before_publish.add(rel_string)
                backup = rollback_root / rel_string
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)

        try:
            # Copy replacements/creations first. Each os.replace is atomic for
            # its file; the rollback backup makes the complete delta atomic from
            # the caller's perspective if a later operation fails.
            for rel_string in changed:
                rel = Path(rel_string)
                source = staged_root / rel
                destination = source_root / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix='.' + destination.name + '.',
                    suffix='.tdwg-process-tmp',
                    dir=str(destination.parent),
                )
                os.close(fd)
                temp_path = Path(temp_name)
                try:
                    shutil.copy2(source, temp_path)
                    os.replace(temp_path, destination)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()

            for rel_string in deleted:
                destination = source_root / rel_string
                if destination.exists():
                    destination.unlink()
        except Exception as publish_error:
            rollback_errors = []
            for rel_string in affected:
                destination = source_root / rel_string
                try:
                    if rel_string in existed_before_publish:
                        backup = rollback_root / rel_string
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        fd, temp_name = tempfile.mkstemp(
                            prefix='.' + destination.name + '.',
                            suffix='.tdwg-process-rollback-tmp',
                            dir=str(destination.parent),
                        )
                        os.close(fd)
                        temp_path = Path(temp_name)
                        try:
                            shutil.copy2(backup, temp_path)
                            os.replace(temp_path, destination)
                        finally:
                            if temp_path.exists():
                                temp_path.unlink()
                    elif destination.exists():
                        destination.unlink()
                except Exception as rollback_error:
                    rollback_errors.append(
                        rel_string + ': ' + str(rollback_error)
                    )

            if rollback_errors:
                raise RuntimeError(
                    'Publishing staged results failed and rollback was incomplete. '
                    'Original publication error: ' + str(publish_error) +
                    '\nRollback errors:\n  - ' + '\n  - '.join(rollback_errors)
                ) from publish_error
            raise RuntimeError(
                'Publishing staged results failed; the repository working tree '
                'was restored to its pre-publication state. Original error: ' +
                str(publish_error)
            ) from publish_error


def _publish_generated_directory(source_root, staged_root, directory_name):
    """Publish generated process output only after metadata commits successfully."""
    source_root = Path(source_root)
    staged_root = Path(staged_root)
    staged_directory = staged_root / 'process' / directory_name
    if not staged_directory.is_dir():
        return

    destination_directory = source_root / 'process' / directory_name
    destination_directory.mkdir(parents=True, exist_ok=True)
    for source in sorted(path for path in staged_directory.iterdir() if path.is_file()):
        destination = destination_directory / source.name
        fd, temp_name = tempfile.mkstemp(
            prefix='.' + destination.name + '.',
            suffix='.tdwg-process-' + directory_name + '-tmp',
            dir=str(destination_directory),
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(source, temp_path)
            os.replace(temp_path, destination)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def _publish_processing_logs(source_root, staged_root):
    """Publish logs produced by a successful staged run."""
    _publish_generated_directory(source_root, staged_root, 'logs')


def _publish_release_reports(source_root, staged_root):
    """Publish GitHub-release-ready reports produced by a successful staged run."""
    _publish_generated_directory(source_root, staged_root, 'reports')


def _run_transactionally_if_needed():
    """Run the full processor in staging and publish only on successful completion."""
    if os.environ.get(_TRANSACTION_ENV) == '1':
        return

    process_dir = Path(__file__).resolve().parent
    repository_root = process_dir.parent
    relative_script = Path(__file__).resolve().relative_to(repository_root)

    with tempfile.TemporaryDirectory(prefix='tdwg-process-') as temp_dir:
        staged_root = Path(temp_dir) / 'repository'
        _copy_repository_for_transaction(repository_root, staged_root)
        before = _tree_manifest(staged_root)

        env = os.environ.copy()
        env[_TRANSACTION_ENV] = '1'
        staged_script = staged_root / relative_script
        result = subprocess.run(
            [sys.executable, staged_script.name],
            cwd=staged_script.parent,
            env=env,
        )
        if result.returncode != 0:
            print(
                'Processing aborted. '
                'The repository working tree was not changed.',
                file=sys.stderr,
            )
            raise SystemExit(result.returncode)

        after = _tree_manifest(staged_root)
        try:
            _publish_transaction_delta(repository_root, staged_root, before, after)
        except Exception as error:
            print(str(error), file=sys.stderr)
            print(
                'Processing completed in staging, but publication was aborted.',
                file=sys.stderr,
            )
            raise SystemExit(3)
        _publish_processing_logs(repository_root, staged_root)
        _publish_release_reports(repository_root, staged_root)
        print('Published successful staged processing results to the repository.')

    raise SystemExit(0)


_run_transactionally_if_needed()

# -----------------------
# Configuration section
# -----------------------

# The mutable values come from a JSON configuration file, config.json, that is in the same directory as
# the script. See the example at:
with open('config.yaml', 'rt', encoding='utf-8') as file_object:
    config = yaml.safe_load(file_object)

# Configuration for vocabulary and standard metadata. If changes are to an existing vocabulary, the values will be ignored.
with open('vocab.yaml', 'rt', encoding='utf-8') as file_object:
    config_vocab = yaml.safe_load(file_object)

# Validate the minimum release configuration before individual values are read.
# This turns missing configuration keys into one explicit preflight diagnostic
# rather than an incidental KeyError.
_required_release_config_keys = {
    'date_issued', 'local_offset_from_utc', 'vocab_type', 'standard',
    'namespaces', 'vocabulary', 'revision_directory',
}
if not isinstance(config, dict):
    raise ValueError('Preflight validation failed: config.yaml must contain a YAML mapping')
_missing_release_config_keys = sorted(_required_release_config_keys - set(config))
if _missing_release_config_keys:
    raise ValueError(
        'Preflight validation failed: config.yaml is missing required keys: ' +
        ', '.join(_missing_release_config_keys)
    )

date_issued = config['date_issued'] # generally will be ratification date
local_offset_from_utc = config['local_offset_from_utc'] # time zone used by system clock
vocab_type = config['vocab_type'] # 1 is simple vocabulary, 2 is simple controlled vocabulary, 3 is c.v. with broader hierarchy
standardUri = config['standard'] # IRI of containing standard
namespaces = config['namespaces'] # list of namespace-specific configuration data
vocabularyIri = config['vocabulary'] # IRI of containing vocabulary
revision_directory = config['revision_directory'] # directory containing dated release-input directories
release_directory = os.path.join(
    revision_directory,
    revision_directory + '-' + str(date_issued)
)

# Run logging. The log is written only after processing completes successfully.
run_started = datetime.datetime.now()
namespace_results = []

def file_digest(path):
    """Return a SHA-256 digest for a file, or None if it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as file_object:
            for chunk in iter(lambda: file_object.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, IOError):
        return None

def snapshot_repository_files():
    """Snapshot repository files so the run log can report files actually created or changed."""
    repository_root = os.path.abspath('..')
    process_generated = {os.path.abspath('logs'), os.path.abspath('reports')}
    snapshot = {}
    for root, dirs, files in os.walk(repository_root):
        # Ignore Git internals, Python caches, and prior run logs.
        dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__'}]
        if any(os.path.abspath(root).startswith(path) for path in process_generated):
            continue
        for filename in files:
            path = os.path.join(root, filename)
            digest = file_digest(path)
            if digest is not None:
                snapshot[path] = digest
    return snapshot

repository_snapshot_before = snapshot_repository_files()

# -------------
# Utility functions
# -------------

def readCsv(filename):
    fileObject = open(filename, 'r', newline='', encoding='utf-8')
    readerObject = csv.reader(fileObject)
    array = []
    for row in readerObject:
        array.append(row)
    fileObject.close()
    return array

def _dedupe_rows(rows):
    """Return rows with exact duplicate data rows removed, preserving first-seen order."""
    if not rows:
        return rows
    result = [rows[0]]
    seen = set()
    for row in rows[1:]:
        key = tuple(row)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result

def _upsert_rows_by_columns(rows, key_columns):
    """Keep one row per key, with the last generated representation winning."""
    if not rows or len(rows) == 1:
        return rows
    header = rows[0]
    indexes = [header.index(column) for column in key_columns]
    last_by_key = {}
    order = []
    for row in rows[1:]:
        key = tuple(row[index] for index in indexes)
        if key not in last_by_key:
            order.append(key)
        last_by_key[key] = row
    return [header] + [last_by_key[key] for key in order]

def _normalize_generated_table(fileName, array):
    """Normalize generated rows so repeated processing preserves stable identities.

    Dated resources are stable identities and relationship tables are sets. A
    rerun therefore replaces the generated representation of an existing identity
    rather than adding another copy.
    """
    rows = _dedupe_rows(array)
    if not rows:
        return rows
    header = rows[0]
    basename = os.path.basename(fileName)

    # Entity/version tables: the stable resource IRI identifies the row.
    if basename == 'decisions.csv' and 'term_localName' in header:
        rows = _upsert_rows_by_columns(rows, ['term_localName'])
    elif basename in {'term-lists.csv'} and 'list' in header:
        rows = _upsert_rows_by_columns(rows, ['list'])
    elif basename in {'vocabularies.csv'} and 'vocabulary' in header:
        rows = _upsert_rows_by_columns(rows, ['vocabulary'])
    elif basename in {'standards.csv'} and 'standard' in header:
        rows = _upsert_rows_by_columns(rows, ['standard'])
    elif 'version' in header and basename.endswith('-versions.csv'):
        # Detailed version-resource tables have a full `version` IRI column.
        # Join tables with the same basename do not use a column literally named `version`.
        rows = _upsert_rows_by_columns(rows, ['version'])

    # A same-release rerun must never assert that a version replaces itself.
    if 'replacements' in basename and len(rows) > 1:
        filtered = [rows[0]]
        for row in rows[1:]:
            if len(row) >= 2:
                replacing, replaced = row[0], row[1]
                # Generic normalization may compare identifiers directly, but
                # must not infer semantic identity by dissecting URI syntax.
                if replacing == replaced:
                    continue
            filtered.append(row)
        rows = _dedupe_rows(filtered)

    return rows

def writeCsv(fileName, array):
    array = _normalize_generated_table(fileName, array)
    fileObject = open(fileName, 'w', newline='', encoding='utf-8')
    writerObject = csv.writer(fileObject, lineterminator=os.linesep)
    for row in array:
        writerObject.writerow(row)
    fileObject.close()

    # returns a list with first item Boolean and second item the index
def findColumnWithHeader(header_row_list, header_label):
    found = False
    for column_number in range(0, len(header_row_list)):
        if header_row_list[column_number] == header_label:
            found = True
            found_column = column_number
    if found:
        return [True, found_column]
    else:
        return [False, 0]
    
def isoTime(offset):
    """Return the deterministic metadata timestamp for the target release.

    Metadata generated for a release must not depend on when the processor is
    executed. The release date is the semantic modification date; midnight is
    used as the canonical time component and the configured release offset is
    retained. Historical timestamps already present in metadata are untouched.
    """
    return date_issued + "T00:00:00" + offset

# -------------
# Core processing functions
# -------------

# This function contains the Step 2 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def generate_and_copy_mapping_and_config_files(vocab_type, namespaceUri, database, modifications_filename):
    # get the mutable column headers from the modifications file
    modifications_metadata = readCsv(modifications_filename)
    mutable_header = modifications_metadata[0][1:len(modifications_metadata[0])]

    # create database directories
    try:
        os.mkdir('../' + database)
        os.mkdir('../' + database + '-versions')
    # do nothing if there is an error (i.e. they already exist)
    except:
        pass

    # copy files needed in the current terms database directory
    endings = ['-classes.csv', '-replacements-classes.csv', '-replacements-column-mappings.csv', '-replacements.csv', '-versions-classes.csv', '-versions-column-mappings.csv', '-versions.csv']
    source_path = 'files_for_new/current_terms/'
    for file_ending in endings:
        source = source_path + 'template' + file_ending
        destination = '../' + database + '/' + database + file_ending
        dest_path = shutil.copyfile(source, destination)
    dest_path = shutil.copyfile(source_path + 'namespace.csv', '../' + database + '/namespace.csv')

    # select current terms column mapping file appropriate for modifications spreadsheet
    if vocab_type == 1: # simple vocabulary
        in_file = 'simple-vocabulary-column-mappings.csv'
    elif vocab_type == 2: # simple controlled vocabulary
        in_file = 'simple-cv-column-mappings.csv'
    elif vocab_type == 3: # c.v. with skos:broader hierarchy
        in_file = 'cv-hierarchy-column-mappings.csv'
    else: # This should not happen
        in_file = 'simple-vocabulary-column-mappings.csv'

    frame = pd.read_csv(source_path + in_file, na_filter=False)
    for index,row in frame.iterrows():
        # replace the placeholder IRIs with the namespace IRI
        if row['header'] == 'skos_inScheme':
            frame.at[index,'value'] = namespaceUri
        if row['header'] == 'skos_broader':
            frame.at[index,'value'] = namespaceUri
    frame.to_csv('../' + database + '/' + database + '-column-mappings.csv', index=False)
        
    # set the core class file and domain root in the constants.csv configuration file
    frame = pd.read_csv(source_path + 'constants.csv', na_filter=False)
    frame.at[0,'domainRoot'] = namespaceUri
    frame.at[0,'coreClassFile'] = database + '.csv'
    frame.to_csv('../' + database + '/constants.csv', index=False)
        
    # set the versions and replacements filenames in the linked-classes.csv file
    frame = pd.read_csv(source_path + 'linked-classes.csv', na_filter=False)
    for index,row in frame.iterrows():
        # replace the placeholder filenames with the actual linked file names
        if row['link_column'] == 'term_localName':
            frame.at[index,'filename'] = database + '-versions.csv'
        if row['link_column'] == 'replaced_term_localName':
            frame.at[index,'filename'] = database + '-replacements.csv'
    frame.to_csv('../' + database + '/linked-classes.csv', index=False)
        
    # create header row for current terms metadata CSV
    current_terms_header = ['document_modified', 'term_localName', 'term_isDefinedBy', 'term_created', 'term_modified', 'term_deprecated', 'replaces_term', 'replaces1_term', 'replaces2_term'] + mutable_header
    current_terms_table = [current_terms_header]
    file_path = '../' + database + '/' + database + '.csv'
    writeCsv(file_path, current_terms_table)


    # copy files needed in the versions database directory
    endings = ['-versions-classes.csv', '-versions-replacements-classes.csv', '-versions-replacements-column-mappings.csv', '-versions-replacements.csv']
    source_path = 'files_for_new/versions/'
    for file_ending in endings:
        source = source_path + 'template' + file_ending
        destination = '../' + database + '-versions/' + database + file_ending
        dest_path = shutil.copyfile(source, destination)
    #dest_path = shutil.copyfile(source_path + 'linked-classes.csv', '../' + database + '-versions/linked-classes.csv')
    dest_path = shutil.copyfile(source_path + 'namespace.csv', '../' + database + '-versions/namespace.csv')

    # select versions column mapping file appropriate for modifications spreadsheet
    if vocab_type == 1: # simple vocabulary
        in_file = 'simple-vocabulary-versions-column-mappings.csv'
    elif vocab_type == 2: # simple controlled vocabulary
        in_file = 'simple-cv-versions-column-mappings.csv'
    elif vocab_type == 3: # c.v. with skos:broader hierarchy
        in_file = 'cv-hierarchy-versions-column-mappings.csv'
    else: # This should not happen
        in_file = 'simple-vocabulary-versions-column-mappings.csv'

    frame = pd.read_csv(source_path + in_file, na_filter=False)
    for index,row in frame.iterrows():
        # replace the placeholder IRIs with the namespace IRI
        if row['header'] == 'skos_inScheme':
            frame.at[index,'value'] = namespaceUri
        if row['header'] == 'skos_broader':
            frame.at[index,'value'] = namespaceUri
        if row['header'] == 'term_localName':
            frame.at[index,'value'] = namespaceUri
    frame.to_csv('../' + database + '-versions/' + database + '-versions-column-mappings.csv', index=False)

    # set the core class file and domain root in the constants.csv configuration file
    frame = pd.read_csv(source_path + 'constants.csv', na_filter=False)
    frame.at[0,'domainRoot'] = namespaceUri + 'version/'
    frame.at[0,'coreClassFile'] = database + '-versions.csv'
    frame.to_csv('../' + database + '-versions/constants.csv', index=False)

    # set the versions and replacements filenames in the linked-classes.csv file
    frame = pd.read_csv(source_path + 'linked-classes.csv', na_filter=False)
    for index,row in frame.iterrows():
        # replace the placeholder filename with the actual linked file name
        if row['link_column'] == 'replaced_version_localName':
            frame.at[index,'filename'] = database + '-versions-replacements.csv'
    frame.to_csv('../' + database + '-versions/linked-classes.csv', index=False)

    # create header row for versions metadata CSV
    versions_header = ['document_modified', 'version', 'versionLocalName', 'version_isDefinedBy', 'version_issued', 'version_status', 'replaces_version', 'replaces1_version', 'replaces2_version'] + mutable_header + ['term_localName']
    versions_table = [versions_header]
    file_path = '../' + database + '-versions/' + database + '-versions.csv'
    writeCsv(file_path, versions_table)

# This function contains the Step 3 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def determine_state_of_data_tables(database, versions, borrowed, utility_namespace, modifications_filename, date_issued):
    # 2.1 read tables
    terms_metadata_filename = '../' + database + '/' + database + '.csv'
    terms_metadata = readCsv(terms_metadata_filename)

    modifications_metadata = readCsv(modifications_filename)

    # find column numbers
    result = findColumnWithHeader(modifications_metadata[0], 'term_localName')
    if result[0] == False:
        print('The modifications file does not have a term_localName column')
        sys.exit()
    else:
        mods_local_name = result[1]

    # don't error trap here because all existing files should have a local name column header
    result = findColumnWithHeader(terms_metadata[0], 'term_localName')
    metadata_localname_column = result[1]

    # create list of local names
    mods_term_localName = []
    for term_number in range(1, len(modifications_metadata)):
        mods_term_localName.append(modifications_metadata[term_number][mods_local_name])

    # Find new and modified terms.  For TDWG-owned versioned terms, use the
    # version history as the authoritative release-state evidence.  A current
    # term is still "new" on a same-release rerun only when the target-release
    # version already exists and there is no version of that term issued before
    # this release.  This avoids using term_created as a proxy for processing
    # state.
    #
    # Borrowed terms deliberately have no TDWG term-version history.  For those
    # terms only, term_created is the persistent rerun signal: this processor
    # sets it when the borrowed term is first added to the local current-terms
    # representation.
    new_terms = []
    modified_terms = []

    term_version_rows = {}
    if not borrowed and not utility_namespace:
        term_versions_metadata = readCsv('../' + versions + '/' + versions + '.csv')
        version_term_local_name = findColumnWithHeader(term_versions_metadata[0], 'term_localName')[1]
        version_issued = findColumnWithHeader(term_versions_metadata[0], 'version_issued')[1]
        for row in term_versions_metadata[1:]:
            term_version_rows.setdefault(row[version_term_local_name], []).append(row[version_issued])

    created_result = findColumnWithHeader(terms_metadata[0], 'term_created')
    metadata_created_column = created_result[1] if created_result[0] else None
    target_date = datetime.date.fromisoformat(date_issued)

    for test_term in mods_term_localName:
        matching_row = None
        for term in terms_metadata[1:]:
            if test_term == term[metadata_localname_column]:
                matching_row = term
                break

        if matching_row is None:
            new_terms.append(test_term)
            continue

        if borrowed or utility_namespace:
            if metadata_created_column is not None and matching_row[metadata_created_column] == date_issued:
                new_terms.append(test_term)
            else:
                modified_terms.append(test_term)
            continue

        has_target_version = False
        has_earlier_version = False
        for issued_value in term_version_rows.get(test_term, []):
            try:
                issued_date = datetime.date.fromisoformat(issued_value)
            except (TypeError, ValueError):
                raise ValueError(
                    'term ' + test_term + ' has an invalid version_issued date "' +
                    str(issued_value) + '".'
                )
            if issued_date == target_date:
                has_target_version = True
            elif issued_date < target_date:
                has_earlier_version = True

        if has_target_version and not has_earlier_version:
            new_terms.append(test_term)
        else:
            modified_terms.append(test_term)

    return terms_metadata, modifications_metadata, mods_local_name, metadata_localname_column, mods_term_localName, new_terms, modified_terms

def find_predecessor_row(metadata, resource_column, resource_value, issued_column, date_issued, context):
    """Return the row index of the latest version issued before date_issued."""
    target_date = datetime.date.fromisoformat(date_issued)
    predecessor_row = None
    predecessor_date = None

    for row_number in range(1, len(metadata)):
        if metadata[row_number][resource_column] != resource_value:
            continue
        issued_value = metadata[row_number][issued_column]
        try:
            issued_date = datetime.date.fromisoformat(issued_value)
        except (TypeError, ValueError):
            raise ValueError(
                context + ' has an invalid issued date "' + str(issued_value) +
                '" in metadata row ' + str(row_number) + '.'
            )
        if issued_date < target_date and (predecessor_date is None or issued_date > predecessor_date):
            predecessor_date = issued_date
            predecessor_row = row_number

    if predecessor_row is None:
        raise ValueError(
            'No predecessor issued before ' + date_issued + ' was found for ' + context + '.'
        )

    return predecessor_row

# This function contains the Step 4 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def generate_term_versions_metadata(database, versions, version_namespace, mods_local_name, modified_terms,local_offset_from_utc, date_issued, modifications_metadata):
    term_versions_metadata_filename = '../' + versions + '/' + versions + '.csv'
    term_versions_metadata = readCsv(term_versions_metadata_filename)

    version_modified = findColumnWithHeader(term_versions_metadata[0], 'document_modified')[1]
    version_column = findColumnWithHeader(term_versions_metadata[0], 'version')[1]
    version_local_name = findColumnWithHeader(term_versions_metadata[0], 'versionLocalName')[1]
    version_isDefinedBy = findColumnWithHeader(term_versions_metadata[0], 'version_isDefinedBy')[1]
    version_issued = findColumnWithHeader(term_versions_metadata[0], 'version_issued')[1]
    version_status = findColumnWithHeader(term_versions_metadata[0], 'version_status')[1]
    replaces_version = findColumnWithHeader(term_versions_metadata[0], 'replaces_version')[1]
    version_term_local_name_column = findColumnWithHeader(term_versions_metadata[0], 'term_localName')[1]

    for column in modifications_metadata[0]:
        result = findColumnWithHeader(term_versions_metadata[0], column)
        if result[0] == False:
            print('The versions file is missing the ', column, ' column.')
            sys.exit()

    versions_join_table_filename = '../' + database + '/' + versions + '.csv'
    versions_join_table = readCsv(versions_join_table_filename)

    newVersions = []
    newVersionJoins = []

    for row_number in range(1, len(modifications_metadata)):
        newVersion = []
        # create a column for every column in the term version file
        for column in term_versions_metadata[0]:
            # find the column in the modifications file that matches the version column and add its value
            result = findColumnWithHeader(modifications_metadata[0], column)
            if result[0] == True:
                newVersion.append(modifications_metadata[row_number][result[1]])
            else:
                newVersion.append('')
        # set the modification dateTime for the newly created version
        newVersion[version_modified] = isoTime(local_offset_from_utc)
        newVersions.append(newVersion)

    for rowNumber in range(0, len(newVersions)):
        # need to add one to the row of modifications_metadata because it includes a header row
        currentTermLocalName = modifications_metadata[rowNumber + 1][mods_local_name]
        newVersions[rowNumber][version_issued] = date_issued
        newVersions[rowNumber][version_status] = 'recommended'
        newVersions[rowNumber][version_local_name] = currentTermLocalName + '-' + date_issued
        newVersions[rowNumber][version_isDefinedBy] = version_namespace
        newVersions[rowNumber][version_column] = version_namespace + currentTermLocalName + '-' + date_issued

        # if the new version replaces an older one for the term, select the
        # latest version issued strictly before this release.
        if currentTermLocalName in modified_terms:
            predecessor_row = find_predecessor_row(
                term_versions_metadata, version_term_local_name_column, currentTermLocalName,
                version_issued, date_issued, 'term ' + currentTermLocalName
            )
            predecessor_version = term_versions_metadata[predecessor_row][version_column]
            newVersions[rowNumber][replaces_version] = predecessor_version
            term_versions_metadata[predecessor_row][version_status] = 'superseded'
            term_versions_metadata[predecessor_row][version_modified] = isoTime(local_offset_from_utc)
        
        # create a join record for each new version and add it to the list of new joins
        newVersionJoin =[ newVersions[rowNumber][version_column], modifications_metadata[rowNumber + 1][mods_local_name] ]
        newVersionJoins.append(newVersionJoin)

    revised_term_versions_metadata = term_versions_metadata + newVersions
    writeCsv('../' + versions + '/' + versions + '.csv', revised_term_versions_metadata)

    revised_term_versions_joins = versions_join_table + newVersionJoins
    writeCsv('../' + database + '/' + versions + '.csv', revised_term_versions_joins)

    versions_replacements_table_filename = '../' + versions + '/' + versions + '-replacements.csv'
    versions_replacements_table = readCsv(versions_replacements_table_filename)

    # create a list to hold the newly generated replacements rows
    newReplacements = []

    for modifiedTerm in modified_terms:
        # generate the newly created version URI for the modified term
        newVersion = version_namespace + modifiedTerm  + '-' + date_issued
        predecessor_row = find_predecessor_row(
            term_versions_metadata, version_term_local_name_column, modifiedTerm,
            version_issued, date_issued, 'term ' + modifiedTerm
        )
        mostRecentLocal = term_versions_metadata[predecessor_row][version_local_name]
        newReplacements.append([newVersion, mostRecentLocal])

    revised_versions_replacements_table = versions_replacements_table + newReplacements
    writeCsv('../' + versions + '/' + versions + '-replacements.csv', revised_versions_replacements_table)

# This function contains the Step 5 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def generate_current_terms_metadata(standardUri, terms_metadata, modifications_metadata, mods_local_name, modified_terms, local_offset_from_utc, date_issued, namespaceUri, termlist_uri, database, versions, term_list_label, term_list_description, pref_namespace_prefix, use_namespace_in_fragment, prepend_url, separator, borrowed):
    # list_localName is the complete path of the term-list IRI, independent of
    # scheme and host (for example, 'dwc/terms/' or 'dwc/terms/attributes/').
    termlist_parts = urllib.parse.urlsplit(termlist_uri)
    list_localname_value = termlist_parts.path.lstrip('/')
    if not list_localname_value.endswith('/'):
        list_localname_value += '/'

    term_modified_dateTime = findColumnWithHeader(terms_metadata[0], 'document_modified')[1]
    term_localName = findColumnWithHeader(terms_metadata[0], 'term_localName')[1]
    term_modified = findColumnWithHeader(terms_metadata[0], 'term_modified')[1]
    term_created = findColumnWithHeader(terms_metadata[0], 'term_created')[1]
    term_isDefinedBy = findColumnWithHeader(terms_metadata[0], 'term_isDefinedBy')[1]

    # Materialize the target-release current state. Release classification
    # (new versus modified) is intentionally separate from whether a current
    # row already exists: on a same-release rerun, a term that was new in this
    # release already has a current row and that row must be updated in place,
    # not appended a second time.
    for mods_rownumber in range(1, len(modifications_metadata)):
        mods_localname_string = modifications_metadata[mods_rownumber][mods_local_name]

        existing_row_number = None
        for term_rownumber in range(1, len(terms_metadata)):
            if mods_localname_string == terms_metadata[term_rownumber][term_localName]:
                existing_row_number = term_rownumber
                break

        if existing_row_number is not None:
            # Existing current row: update it in place. This covers both a
            # genuinely modified term and a term that was new in this release
            # but is being recomputed on a same-release rerun.
            terms_metadata[existing_row_number][term_modified_dateTime] = isoTime(local_offset_from_utc)
            terms_metadata[existing_row_number][term_modified] = date_issued
            for column_number in range(0, len(modifications_metadata[0])):
                result = findColumnWithHeader(terms_metadata[0], modifications_metadata[0][column_number])
                if result[0] == True:
                    terms_metadata[existing_row_number][result[1]] = modifications_metadata[mods_rownumber][column_number]
        else:
            # No current row exists: materialize a genuinely new term.
            newTermRow = ['' for _ in range(0, len(terms_metadata[0]))]
            newTermRow[term_modified_dateTime] = isoTime(local_offset_from_utc)
            newTermRow[term_modified] = date_issued
            newTermRow[term_created] = date_issued
            newTermRow[term_isDefinedBy] = namespaceUri
            for column_number in range(0, len(modifications_metadata[0])):
                result = findColumnWithHeader(terms_metadata[0], modifications_metadata[0][column_number])
                if result[0] == True:
                    newTermRow[result[1]] = modifications_metadata[mods_rownumber][column_number]
            terms_metadata.append(newTermRow)
    writeCsv('../' + database + '/' + database + '.csv', terms_metadata)

    # Section 5 for generating new term lists
    term_lists_table_filename = '../term-lists/term-lists.csv'
    term_lists_table = readCsv(term_lists_table_filename)

    term_lists_versions_joins_filename = '../term-lists/term-lists-versions.csv'
    term_lists_versions_joins = readCsv(term_lists_versions_joins_filename)

    term_lists_members_filename = '../term-lists/term-lists-members.csv'
    term_lists_members = readCsv(term_lists_members_filename)

    term_lists_versions_metadata_filename = '../term-lists-versions/term-lists-versions.csv'
    term_lists_versions_metadata = readCsv(term_lists_versions_metadata_filename)

    term_lists_versions_members_filename = '../term-lists-versions/term-lists-versions-members.csv'
    term_lists_versions_members = readCsv(term_lists_versions_members_filename)

    term_lists_versions_replacements_filename = '../term-lists-versions/term-lists-versions-replacements.csv'
    term_lists_versions_replacements = readCsv(term_lists_versions_replacements_filename)

    datasets_index_filename = '../index/index-datasets.csv'
    datasets_index = readCsv(datasets_index_filename)

    # Construct the term-list version IRI according to the rs.tdwg.org convention:
    # insert 'version' after the first path component and append the issue date.
    # This handles both ordinary lists (dwc/terms/) and deeper list paths
    # (dwc/terms/attributes/) without fixed slash positions or special cases.
    termlist_path_parts = [part for part in termlist_parts.path.split('/') if part]
    termlist_version_path = '/' + '/'.join(
        [termlist_path_parts[0], 'version'] + termlist_path_parts[1:] + [date_issued]
    )
    termlistVersionUri = urllib.parse.urlunsplit((
        termlist_parts.scheme, termlist_parts.netloc, termlist_version_path, '', ''
    ))

    modified_datetime = findColumnWithHeader(term_lists_table[0], 'document_modified')[1]
    list_uri = findColumnWithHeader(term_lists_table[0], 'list')[1]
    list_localName_column = findColumnWithHeader(term_lists_table[0], 'list_localName')[1]
    list_label = findColumnWithHeader(term_lists_table[0], 'label')[1]
    list_description = findColumnWithHeader(term_lists_table[0], 'description')[1]
    list_created = findColumnWithHeader(term_lists_table[0], 'list_created')[1]
    list_modified = findColumnWithHeader(term_lists_table[0], 'list_modified')[1]
    list_prefix_column = findColumnWithHeader(term_lists_table[0], 'vann_preferredNamespacePrefix')[1]
    list_pref_namespace_column = findColumnWithHeader(term_lists_table[0], 'vann_preferredNamespaceUri')[1]
    list_database_column = findColumnWithHeader(term_lists_table[0], 'database')[1]
    list_versions_database_column = findColumnWithHeader(term_lists_table[0], 'versions_database')[1]
    list_versions_uri_column = findColumnWithHeader(term_lists_table[0], 'versions_uri')[1]
    standard_column = findColumnWithHeader(term_lists_table[0], 'standard')[1]

    aNewTermList = True
    for rowNumber in range(1, len(term_lists_table)):
        # by convention, the namespace URI used for the terms is the same as the URI of the term list
        # Note 2022-04-20: I think that is only true for TDWG-minted term lists, not borrowed ones!
        if termlist_uri == term_lists_table[rowNumber][list_uri]:
            aNewTermList = False
            term_list_rowNumber = rowNumber
            # This function is called only when this term list actually changes in
            # the release, so update modification metadata at the term-list level.
            term_lists_table[rowNumber][list_modified] = date_issued
            term_lists_table[rowNumber][modified_datetime] = isoTime(local_offset_from_utc)
            # here is the opportunity to find out the standard URI for the modified term list
            # 2024-03-01 note: this is now provided in the config.yaml file.
            #standardUri = term_lists_table[rowNumber][standard_column]
            # print(term_lists_table[rowNumber])
    if aNewTermList:  # this will happen if the term list did not previously exist
        # Create a new row for the term list table that is a list with length equal to the 0th row of the table
        new_term_list_row = [''] * len(term_lists_table[0])
        
        """
        try:
            new_term_list = readCsv('files_for_new/new_term_list.csv')
        except:
            print('The term list was not found and there was no new_term_list.csv file.')
            sys.exit()
        """
        # Note: no error trapping is done here, so make sure that the new_term_list columns are the same as term_lists_table
        new_term_list_row[modified_datetime] = isoTime(local_offset_from_utc)
        new_term_list_row[list_uri] = termlist_uri
        new_term_list_row[list_localName_column] = list_localname_value
        new_term_list_row[list_created] = date_issued
        new_term_list_row[list_modified] = date_issued
        new_term_list_row[list_prefix_column] = pref_namespace_prefix
        new_term_list_row[list_pref_namespace_column] = namespaceUri
        new_term_list_row[list_database_column] = database
        new_term_list_row[list_versions_database_column] = versions
        new_term_list_row[list_versions_uri_column] = termlistVersionUri
        new_term_list_row[standard_column] = standardUri

        # This is now about the only value that's dependent on filling out the new_term_list.csv file.
        # 2024-03-01 note: this is now provided in the config.yaml file.
        #standardUri = new_term_list[1][standard_column]

        """
        # Assign the label and description passed into the function if not empty string. Otherwise, fall back on what's 
        # already in the new term list table.
        if term_list_label != '':
            new_term_list_row[list_label] = term_list_label
        if term_list_description != '':
            new_term_list_row[list_description] = term_list_description
        """

        # Label and description are now required in the config.yaml file, so no need to check for empty strings.
        new_term_list_row[list_label] = term_list_label
        new_term_list_row[list_description] = term_list_description

        # the length of the table (including header row) will be one more than the last row number
        term_list_rowNumber = len(term_lists_table)
        term_lists_table.append(new_term_list_row)
        # after the new row is appended, its row number will be one more than the previous last row number

        # The new term list's dataset directory must be added to the dataset list. 
        row_for_current_terms = [isoTime(local_offset_from_utc), # document_modified
                                database, # term_localName
                                'http://rs.tdwg.org/index', # dcterms_isPartOf
                                'http://rs.tdwg.org/index/' + database, # dataset_iri
                                date_issued, # dcterms_modified
                                new_term_list_row[list_description], # label
                                ''] # rdfs_comment
        datasets_index.append(row_for_current_terms)
        
        # New term lists will always have a new version dataset directory, so add it, too.
        row_for_versions = [isoTime(local_offset_from_utc), # document_modified
                            versions, # term_localName
                            'http://rs.tdwg.org/index', # dcterms_isPartOf
                            'http://rs.tdwg.org/index/' + versions, # dataset_iri
                            date_issued, # dcterms_modified
                            new_term_list_row[list_description] + ' versions', # label
                            ''] # rdfs_comment
        datasets_index.append(row_for_versions)
        
    else:
        # This function is called only when this namespace has actual term changes.
        # Update only datasets changed at this level. Borrowed namespaces do not
        # generate TDWG term-version metadata, so their versions dataset is not
        # marked modified merely because their current-term dataset changed.
        for dataset_rownumber in range(1, len(datasets_index)):
            dataset_name = datasets_index[dataset_rownumber][1]
            if database == dataset_name:
                datasets_index[dataset_rownumber][0] = isoTime(local_offset_from_utc)
                datasets_index[dataset_rownumber][4] = date_issued
            if not borrowed and versions == dataset_name:
                datasets_index[dataset_rownumber][0] = isoTime(local_offset_from_utc)
                datasets_index[dataset_rownumber][4] = date_issued

    # The Term List and Term List Version metadata datasets are changed by this
    # function whenever it is called. Higher-level Vocabulary and Standard
    # dataset dates are updated by their own processing functions.
    for dataset_rownumber in range(1, len(datasets_index)):
        dataset_name = datasets_index[dataset_rownumber][1]
        if dataset_name in ('term-lists', 'term-lists-versions'):
            datasets_index[dataset_rownumber][0] = isoTime(local_offset_from_utc)
            datasets_index[dataset_rownumber][4] = date_issued
            
    writeCsv('../term-lists/term-lists.csv', term_lists_table)
    writeCsv('../index/index-datasets.csv', datasets_index)

    term_lists_versions_joins.append([termlistVersionUri, termlist_uri])
    writeCsv('../term-lists/term-lists-versions.csv', term_lists_versions_joins)

    for newTerm in new_terms:
        term_lists_members.append([termlist_uri, namespaceUri + newTerm])
    writeCsv('../term-lists/term-lists-members.csv', term_lists_members)

    # find the columns than contain needed information
    document_modified = findColumnWithHeader(term_lists_versions_metadata[0], 'document_modified')[1]
    version_uri = findColumnWithHeader(term_lists_versions_metadata[0], 'version')[1]
    version_modified = findColumnWithHeader(term_lists_versions_metadata[0], 'version_modified')[1]
    status_column = findColumnWithHeader(term_lists_versions_metadata[0], 'status')[1]
    localname_column = findColumnWithHeader(term_lists_versions_metadata[0], 'list_localName')[1]
    list_version_label = findColumnWithHeader(term_lists_versions_metadata[0], 'label')[1]
    list_version_description = findColumnWithHeader(term_lists_versions_metadata[0], 'description')[1]
    list_version_prefix_column = findColumnWithHeader(term_lists_versions_metadata[0], 'vann_preferredNamespacePrefix')[1]
    list_version_namespace_uri_column = findColumnWithHeader(term_lists_versions_metadata[0], 'vann_preferredNamespaceUri')[1]
    list_uri = findColumnWithHeader(term_lists_versions_metadata[0], 'list')[1]

    if aNewTermList:
        # Create a new row for the term list table that is a list with length equal to the 0th row of the table

        """
        # get the template for the term list version from first data row in the new_term_list_version.csv file
        try:
            new_term_list_version = readCsv('files_for_new/new_term_list_version.csv')
        except:
            print('The term list version was not found and there was no new_term_list_version.csv file.')
            sys.exit()
        """
        #newListRow = new_term_list_version[1]

        mostRecentListNumber = None # no predecessor exists for a new term list

        # Label, description, and pref prefix are now required in the config.yaml file, so no need to check for empty strings.
        """
        # Assign the label, description, and pref prefix passed into the function if not empty string. Otherwise, fall back on what's 
        # already in the new term list version table.
        if term_list_label != '':
            newListRow[list_version_label] = term_list_label
        if term_list_description != '':
            newListRow[list_version_description] = term_list_description
        if pref_namespace_prefix != '':
            newListRow[list_version_prefix_column] = pref_namespace_prefix
        """

    else:
        # Find the latest previous version by its explicit version date.
        mostRecentListNumber = find_predecessor_row(
            term_lists_versions_metadata, list_uri, termlist_uri,
            version_modified, date_issued, 'term list ' + termlist_uri
        )

        # change the status of the most recent list to superseded
        term_lists_versions_metadata[mostRecentListNumber][status_column] = 'superseded'
        term_lists_versions_metadata[mostRecentListNumber][document_modified] = isoTime(local_offset_from_utc)

        # start the new list row with the metadata from the most recent list
        #newListRow = copy.deepcopy(term_lists_versions_metadata[mostRecentListNumber])

    newListRow = [''] * len(term_lists_versions_metadata[0])

    # Insert metadata to make the most recent list have the updated values
    newListRow[document_modified] = isoTime(local_offset_from_utc)
    newListRow[version_uri] = termlistVersionUri
    newListRow[version_modified] = date_issued
    newListRow[status_column] = 'recommended'
    newListRow[localname_column] = list_localname_value
    newListRow[list_version_label] = term_list_label
    newListRow[list_version_description] = term_list_description
    newListRow[list_version_prefix_column] = pref_namespace_prefix
    newListRow[list_version_namespace_uri_column] = namespaceUri
    newListRow[list_uri] = termlist_uri

    # append the new term list row to the old list of term lists
    term_lists_versions_metadata.append(newListRow)

    # save as a file
    writeCsv('../term-lists-versions/term-lists-versions.csv', term_lists_versions_metadata)

    # -----------------
    # Code added 2024-03-03 to update the redirects file that controls redirects for current terms for this namespace.
    # The redirect record is in the repo_path + 'html/redirects.csv' file.

    redirects_df = pd.read_csv('../html/redirects.csv', dtype=str)
    
    # Create a row for namespace redirect
    if use_namespace_in_fragment:
        use_namespace = 'yes'
        connector = separator
    else:
        use_namespace = 'no'
        connector = ''
    term_redirects_row_data = {'database': database, 'redirect': 'yes', 'type': 'term', 'namespace': pref_namespace_prefix, 'prefix': prepend_url, 'useNamespace': use_namespace, 'connector': connector}

    # Find the row index for the namespace redirect in the pandas dataframe and replace it with the new data.
    # If the row is not found, add it to the end of the pandasdataframe.
    matching_rows_index = redirects_df[redirects_df['database'] == database].index
    if len(matching_rows_index) > 1:
        print('Error: More than one row found for the namespace redirect in the redirects.csv file.')
        sys.exit()
    elif len(matching_rows_index) == 1:
        # replace the row with the new data
        redirects_df.loc[matching_rows_index[0]] = term_redirects_row_data
    else:
        # add the row to the end of the dataframe
        redirects_df = pd.concat([redirects_df, pd.DataFrame([term_redirects_row_data])])

    # Create row for term version redirect
    version_redirects_row_data = {'database': database + '-versions', 'redirect': 'no', 'type': 'termVersion', 'namespace': pref_namespace_prefix, 'prefix': '', 'useNamespace': '', 'connector': ''}
    matching_rows_index = redirects_df[redirects_df['database'] == database + '-versions'].index
    if len(matching_rows_index) > 1:
        print('Error: More than one row found for the term version redirect in the redirects.csv file.')
        sys.exit()
    elif len(matching_rows_index) == 1:
        # replace the row with the new data
        redirects_df.loc[matching_rows_index[0]] = version_redirects_row_data
    else:
        # add the row to the end of the dataframe
        redirects_df = pd.concat([redirects_df, pd.DataFrame([version_redirects_row_data])])

    redirects_df.to_csv('../html/redirects.csv', index = False)

    return version_uri, aNewTermList, term_lists_versions_members, term_lists_versions_metadata, mostRecentListNumber, termlistVersionUri, term_lists_versions_replacements, term_lists_table, term_list_rowNumber

# This function contains the Step 6 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def update_termlist_version_members(aNewTermList, mostRecentListNumber, date_issued, namespaceUri, database, new_terms, modified_terms, version_uri, termlistVersionUri, term_lists_versions_metadata, term_lists_versions_members, term_lists_versions_replacements):
    # create a list of every term version that was in the most recent previous list version
    newTermVersionMembersList = []
    # create a corresponding list of local names for those versions
    termLocalNameList = []

    if not aNewTermList:
        # Resolve term-version IRIs to term local names through the namespace's
        # version join table rather than parsing local names out of the IRIs.
        versions_join_table = readCsv('../' + database + '/' + database + '-versions.csv')
        term_local_name_by_version_uri = {row[0]: row[1] for row in versions_join_table[1:]}
        predecessor_version_uri = term_lists_versions_metadata[mostRecentListNumber][version_uri]

        for termVersion in term_lists_versions_members:
            # the first column contains the term list version
            if predecessor_version_uri == termVersion[0]:
                member_version_uri = termVersion[1]
                if member_version_uri not in term_local_name_by_version_uri:
                    raise ValueError(
                        'Term List version ' + predecessor_version_uri +
                        ' contains member ' + member_version_uri +
                        ', which cannot be resolved to a term_localName in ../' +
                        database + '/' + database + '-versions.csv.'
                    )
                newTermVersionMembersList.append(member_version_uri)
                termLocalNameList.append(term_local_name_by_version_uri[member_version_uri])

        # A Term List version must contain at most one version of any term.
        duplicate_local_names = sorted({
            local_name for local_name in termLocalNameList
            if termLocalNameList.count(local_name) > 1
        })
        if duplicate_local_names:
            raise ValueError(
                'Term List version ' + predecessor_version_uri +
                ' contains more than one member version for term(s): ' +
                ', '.join(duplicate_local_names) + '.'
            )

        # A modified term must occur exactly once in the predecessor membership.
        for modified_term in modified_terms:
            matching_indexes = [
                index for index, local_name in enumerate(termLocalNameList)
                if local_name == modified_term
            ]
            if len(matching_indexes) != 1:
                raise ValueError(
                    'Modified term ' + modified_term + ' must occur exactly once in '
                    'predecessor Term List version ' + predecessor_version_uri +
                    '; found ' + str(len(matching_indexes)) + ' memberships.'
                )
            termVersionRowNumber = matching_indexes[0]
            newTermVersionMembersList[termVersionRowNumber] = (
                namespaceUri + 'version/' + modified_term + '-' + date_issued
            )

        # A term classified as new must not already be a member of the predecessor.
        for new_term in new_terms:
            if new_term in termLocalNameList:
                raise ValueError(
                    'New term ' + new_term + ' is already a member of predecessor '
                    'Term List version ' + predecessor_version_uri + '.'
                )

    # For each newly added term, add its new version to the list.
    for new_term in new_terms:
        newTermVersionMembersList.append(namespaceUri + 'version/' + new_term + '-' + date_issued)
        termLocalNameList.append(new_term)

    # Verify the resulting membership before writing it.
    duplicate_local_names = sorted({
        local_name for local_name in termLocalNameList
        if termLocalNameList.count(local_name) > 1
    })
    if duplicate_local_names:
        raise ValueError(
            'Generated Term List version ' + termlistVersionUri +
            ' would contain more than one member version for term(s): ' +
            ', '.join(duplicate_local_names) + '.'
        )

    # Now that the list is created of new term versions that are part of the new term version list,
    # add a record for each one to the term list versions members table
    for termVersionMember in newTermVersionMembersList:
        term_lists_versions_members.append([termlistVersionUri, termVersionMember])

    # Write the updated term list versions members table to a file
    writeCsv('../term-lists-versions/term-lists-versions-members.csv', term_lists_versions_members)

    if not aNewTermList:
        term_lists_versions_replacements.append([termlistVersionUri, term_lists_versions_metadata[mostRecentListNumber][version_uri]])
        writeCsv('../term-lists-versions/term-lists-versions-replacements.csv', term_lists_versions_replacements)

def update_dataset_index_modified(dataset_names, date_issued, local_offset_from_utc):
    """Mark only the named metadata datasets as modified for this release."""
    filename = '../index/index-datasets.csv'
    table = readCsv(filename)
    names = set(dataset_names)
    for row_number in range(1, len(table)):
        if table[row_number][1] in names:
            table[row_number][0] = isoTime(local_offset_from_utc)
            table[row_number][4] = date_issued
    writeCsv(filename, table)


# This function contains the Step 7 cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def update_vocabulary_metadata(date_issued, local_offset_from_utc, term_lists_table, term_list_rowNumber, termlistVersionUri, vocabularyIri, aNewTermList, termlist_uri):
    vocabularies_table_filename = '../vocabularies/vocabularies.csv'
    vocabularies_table = readCsv(vocabularies_table_filename)

    vocabularies_versions_joins_filename = '../vocabularies/vocabularies-versions.csv'
    vocabularies_versions_joins = readCsv(vocabularies_versions_joins_filename)

    vocabularies_members_filename = '../vocabularies/vocabularies-members.csv'
    vocabularies_members = readCsv(vocabularies_members_filename)

    vocabularies_versions_metadata_filename = '../vocabularies-versions/vocabularies-versions.csv'
    vocabularies_versions_metadata = readCsv(vocabularies_versions_metadata_filename)

    vocabularies_versions_members_filename = '../vocabularies-versions/vocabularies-versions-members.csv'
    vocabularies_versions_members = readCsv(vocabularies_versions_members_filename)

    vocabularies_versions_replacements_filename = '../vocabularies-versions/vocabularies-versions-replacements.csv'
    vocabularies_versions_replacements = readCsv(vocabularies_versions_replacements_filename)

    # Resolve term-list-version IRIs through their metadata rather than by URI shape.
    term_lists_versions_metadata = readCsv('../term-lists-versions/term-lists-versions.csv')
    tlv_version_column = findColumnWithHeader(term_lists_versions_metadata[0], 'version')[1]
    tlv_localname_column = findColumnWithHeader(term_lists_versions_metadata[0], 'list_localName')[1]
    term_list_local_name_by_version_uri = {
        row[tlv_version_column]: row[tlv_localname_column].rstrip('/')
        for row in term_lists_versions_metadata[1:]
    }

    # Get the term-list subpath from the term-list metadata. This identifies which
    # term-list version must be replaced within the containing vocabulary version.
    list_localName_column = findColumnWithHeader(term_lists_table[0], 'list_localName')[1]
    list_localName = term_lists_table[term_list_rowNumber][list_localName_column]
    # Preserve the complete term-list identity (for example, "dwc/terms",
    # "eco/terms", or "chrono/iri"). The final component alone is not unique
    # when term lists from several namespace families belong to one vocabulary.
    termList_subpath = list_localName.rstrip('/')

    # The containing vocabulary is stated explicitly in config.yaml rather than
    # inferred from the term-list IRI. This permits term lists such as eco/terms/
    # and chrono/terms/ to belong to the Darwin Core vocabulary.
    vocabularyUri = vocabularyIri

    # The vocabulary subpath is still needed when matching vocabulary versions in
    # standard metadata. Derive it from the parsed path of the configured
    # vocabulary IRI rather than by splitting the complete URI string.
    vocabulary_path_parts = [
        part for part in urllib.parse.urlsplit(vocabularyUri).path.split('/')
        if part
    ]
    if not vocabulary_path_parts:
        raise ValueError(
            'Configured vocabulary IRI has no path component: ' + vocabularyUri
        )
    vocab_subpath = vocabulary_path_parts[-1]

    # Generate the vocabulary version URI from the configured vocabulary.
    vocabularyVersionUri = 'http://rs.tdwg.org/version/' + vocab_subpath + '/' + date_issued

    # check for the case where the script was previously run to update a different term list in the same new vocabulary version
    temp = findColumnWithHeader(vocabularies_versions_metadata[0], 'version')[1]
    alreadyAddedVocab = False
    for versionRow in vocabularies_versions_metadata:
        if versionRow[temp] == vocabularyVersionUri:
            alreadyAddedVocab = True

    modified_datetime = findColumnWithHeader(vocabularies_table[0], 'document_modified')[1]
    vocabulary_uri = findColumnWithHeader(vocabularies_table[0], 'vocabulary')[1]
    vocabulary_localName_column = findColumnWithHeader(vocabularies_table[0], 'vocabulary_localName')[1]
    vocabulary_label = findColumnWithHeader(vocabularies_table[0], 'label')[1]
    vocabulary_description = findColumnWithHeader(vocabularies_table[0], 'description')[1]
    vocabulary_created = findColumnWithHeader(vocabularies_table[0], 'vocabulary_created')[1]
    vocabulary_modified = findColumnWithHeader(vocabularies_table[0], 'vocabulary_modified')[1]
    vocabulary_dc_creator_column = findColumnWithHeader(vocabularies_table[0], 'dc_creator')[1]
    vocabulary_dcterms_license_column = findColumnWithHeader(vocabularies_table[0], 'dcterms_license')[1]

    aNewVocabulary = True
    for rowNumber in range(1, len(vocabularies_table)):
        if vocabularyUri == vocabularies_table[rowNumber][vocabulary_uri]:
            aNewVocabulary = False
            vocabulary_rowNumber = rowNumber
            # In the case where changes are made to a second term list of a new vocabulary, the new modified date will be the same as before
            vocabularies_table[rowNumber][vocabulary_modified] = date_issued
            vocabularies_table[rowNumber][modified_datetime] = isoTime(local_offset_from_utc)

            # Update the vocabulary_label, vocabulary_description, vocabulary_dc_creator_column, and vocabulary_dcterms_license columns from the vocabulary configuration file
            vocabularies_table[rowNumber][vocabulary_label] = config_vocab['vocabulary_label']
            vocabularies_table[rowNumber][vocabulary_description] = config_vocab['vocabulary_description']
            vocabularies_table[rowNumber][vocabulary_dc_creator_column] = config_vocab['dc_creator']
            vocabularies_table[rowNumber][vocabulary_dcterms_license_column] = config_vocab['dcterms_license']

    if aNewVocabulary: # this will happen if the vocabulary did not previously exist
        """ 
        try:
            new_vocabulary_row = readCsv('files_for_new/new_vocabulary.csv')[1]
        except:
            print('The vocabulary was not found and there was no new_vocabulary.csv file.')
            sys.exit()
        new_vocabulary_row[vocabulary_created] = date_issued
        new_vocabulary_row[vocabulary_modified] = date_issued
        new_vocabulary_row[modified_datetime] = isoTime(local_offset_from_utc)
        vocabularies_table.append(new_vocabulary_row)
        """
        # Create a new row for the vocabulary table that is a list with length equal to the 0th row of the table
        new_vocabulary_row = [''] * len(vocabularies_table[0])

        new_vocabulary_row[modified_datetime] = isoTime(local_offset_from_utc)

        # Assign the vocabulary URI to the new vocabulary row
        new_vocabulary_row[vocabulary_uri] = vocabularyUri

        # Generate the vocabulary local name from the terminal path component
        # already derived from the configured vocabulary IRI.
        vocabularyLocalName = vocab_subpath + '/'
        new_vocabulary_row[vocabulary_localName_column] = vocabularyLocalName

        # Assign the vocabulary label from the vocabulary configuration file to the new vocabulary row
        new_vocabulary_row[vocabulary_label] = config_vocab['vocabulary_label']

        # Assign the vocabulary description from the vocabulary configuration file to the new vocabulary row
        new_vocabulary_row[vocabulary_description] = config_vocab['vocabulary_description']

        # Assign the created and modified dates to the new vocabulary row
        new_vocabulary_row[vocabulary_created] = date_issued
        new_vocabulary_row[vocabulary_modified] = date_issued

        # Assign the creator and license from the vocabulary configuration file to the new vocabulary row
        new_vocabulary_row[vocabulary_dc_creator_column] = config_vocab['dc_creator']
        new_vocabulary_row[vocabulary_dcterms_license_column] = config_vocab['dcterms_license']

        # Append the new vocabulary row to the table
        vocabularies_table.append(new_vocabulary_row)

    writeCsv('../vocabularies/vocabularies.csv', vocabularies_table)

    if not alreadyAddedVocab:
        vocabularies_versions_joins.append([vocabularyVersionUri, vocabularyUri])
        writeCsv('../vocabularies/vocabularies-versions.csv', vocabularies_versions_joins)

    # The unversioned membership records current containment, independently of
    # whether the Term List itself is new in this release. Ensure that the
    # Vocabulary -> Term List relationship exists exactly once.
    matching_vocabulary_memberships = [
        row_number
        for row_number in range(1, len(vocabularies_members))
        if (vocabularies_members[row_number][0] == vocabularyUri and
            vocabularies_members[row_number][1] == termlist_uri)
    ]
    if len(matching_vocabulary_memberships) > 1:
        raise ValueError(
            'Vocabulary ' + vocabularyUri + ' contains duplicate unversioned '
            'membership for Term List ' + termlist_uri + '.'
        )
    if not matching_vocabulary_memberships:
        vocabularies_members.append([vocabularyUri, termlist_uri])
    writeCsv('../vocabularies/vocabularies-members.csv', vocabularies_members)

    # find the columns than contain needed information
    document_modified = findColumnWithHeader(vocabularies_versions_metadata[0], 'document_modified')[1]
    version_uri = findColumnWithHeader(vocabularies_versions_metadata[0], 'version')[1]
    version_issued = findColumnWithHeader(vocabularies_versions_metadata[0], 'version_issued')[1]
    status_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'vocabulary_status')[1]
    label_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'label')[1]
    description_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'description')[1]
    vocabulary_uri = findColumnWithHeader(vocabularies_versions_metadata[0], 'vocabulary')[1]
    creator_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'dc_creator')[1]
    license_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'dcterms_license')[1]

    if not alreadyAddedVocab:
        # Create a new empty row for the vocabulary versions table that is a list with length equal to the 0th row of the table
        newVocabularyRow = [''] * len(vocabularies_versions_metadata[0])

        if aNewVocabulary: # this will happen if the vocabulary did not previously exist
            pass
            """
            try:
                newVocabularyRow = readCsv('files_for_new/new_vocabulary_version.csv')[1]
            except:
                print('The vocabulary version was not found and there was no new_vocabulary_version.csv file.')
                sys.exit()
            
            # the new row will be added to the end and therefore will have an index number - number of rows before appending
            mostRecentVocabularyNumber = len(vocabularies_versions_metadata)
            """
        else:
            # Find the latest previous version by its explicit issued date.
            mostRecentVocabularyNumber = find_predecessor_row(
                vocabularies_versions_metadata, vocabulary_uri, vocabularyUri,
                version_issued, date_issued, 'vocabulary ' + vocabularyUri
            )

            # change the status of the most recent vocabulary to superseded
            vocabularies_versions_metadata[mostRecentVocabularyNumber][status_column] = 'superseded'
            vocabularies_versions_metadata[mostRecentVocabularyNumber][document_modified] = isoTime(local_offset_from_utc)

            # start the new vocabulary row with the metadata from the most recent vocabulary
            #newVocabularyRow = copy.deepcopy(vocabularies_versions_metadata[mostRecentVocabularyNumber])

        # Insert metadata into the new vocabulary row
        newVocabularyRow[document_modified] = isoTime(local_offset_from_utc)
        newVocabularyRow[version_uri] = vocabularyVersionUri
        newVocabularyRow[version_issued] = date_issued
        newVocabularyRow[status_column] = 'recommended'
        newVocabularyRow[label_column] = config_vocab['vocabulary_label']
        newVocabularyRow[description_column] = config_vocab['vocabulary_description']
        newVocabularyRow[vocabulary_uri] = vocabularyUri
        newVocabularyRow[creator_column] = config_vocab['dc_creator']
        newVocabularyRow[license_column] = config_vocab['dcterms_license']

        # append the new term list row to the old list of term lists
        vocabularies_versions_metadata.append(newVocabularyRow)

        # save as a file
        writeCsv('../vocabularies-versions/vocabularies-versions.csv', vocabularies_versions_metadata)

    # Finding #29: construct the target Vocabulary version as a complete snapshot
    # of the Vocabulary's current declared Term List membership. The predecessor
    # snapshot is historical evidence, not the authority for current membership.
    tlv_list_column = findColumnWithHeader(term_lists_versions_metadata[0], 'list')[1]
    tlv_date_column = findColumnWithHeader(term_lists_versions_metadata[0], 'version_modified')[1]

    declared_term_lists = [
        row[1]
        for row in vocabularies_members[1:]
        if row[0] == vocabularyUri
    ]
    duplicate_declared_term_lists = sorted({
        identity for identity in declared_term_lists
        if declared_term_lists.count(identity) > 1
    })
    if duplicate_declared_term_lists:
        raise ValueError(
            'Vocabulary ' + vocabularyUri +
            ' contains duplicate current Term List membership(s): ' +
            ', '.join(duplicate_declared_term_lists) + '.'
        )

    target_date = datetime.date.fromisoformat(date_issued)

    def applicable_term_list_version(term_list_uri):
        candidates = []
        for row in term_lists_versions_metadata[1:]:
            if row[tlv_list_column] != term_list_uri:
                continue
            try:
                candidate_date = datetime.date.fromisoformat(row[tlv_date_column])
            except (TypeError, ValueError):
                raise ValueError(
                    'Term List ' + term_list_uri + ' has invalid version_modified date ' +
                    repr(row[tlv_date_column]) + '.'
                )
            if candidate_date <= target_date:
                candidates.append((candidate_date, row[tlv_version_column]))
        if not candidates:
            raise ValueError(
                'Vocabulary ' + vocabularyUri + ' declares Term List ' +
                term_list_uri + ', but no Term List version exists on or before ' +
                date_issued + '.'
            )
        latest_date = max(candidate[0] for candidate in candidates)
        latest = [uri for candidate_date, uri in candidates if candidate_date == latest_date]
        if len(latest) != 1:
            raise ValueError(
                'Term List ' + term_list_uri + ' has ' + str(len(latest)) +
                ' applicable versions dated ' + latest_date.isoformat() + '.'
            )
        return latest[0]

    resolved_vocabulary_members = [
        applicable_term_list_version(term_list_uri)
        for term_list_uri in declared_term_lists
    ]

    # Rebuild the target-release rows wholesale. This makes same-release reruns
    # converge on the authoritative current membership and removes stale rows
    # left by an earlier pass through another namespace.
    vocabularies_versions_members = [vocabularies_versions_members[0]] + [
        row for row in vocabularies_versions_members[1:]
        if row[0] != vocabularyVersionUri
    ]
    for member_version_uri in resolved_vocabulary_members:
        vocabularies_versions_members.append([vocabularyVersionUri, member_version_uri])

    # Exact-composition invariant: the target snapshot represents every declared
    # current Term List exactly once and no undeclared Term List.
    generated_identities = [
        term_lists_versions_metadata[next(
            row_number for row_number in range(1, len(term_lists_versions_metadata))
            if term_lists_versions_metadata[row_number][tlv_version_column] == member_uri
        )][tlv_list_column]
        for member_uri in resolved_vocabulary_members
    ]
    if generated_identities != declared_term_lists:
        raise ValueError(
            'Generated Vocabulary version ' + vocabularyVersionUri +
            ' does not exactly represent current Vocabulary membership.'
        )

    writeCsv('../vocabularies-versions/vocabularies-versions-members.csv', vocabularies_versions_members)

    if not(aNewVocabulary) and not(alreadyAddedVocab):
        vocabularies_versions_replacements.append([vocabularyVersionUri, vocabularies_versions_metadata[mostRecentVocabularyNumber][version_uri]])
        writeCsv('../vocabularies-versions/vocabularies-versions-replacements.csv', vocabularies_versions_replacements)
    # This function changes the Vocabulary and Vocabulary Version metadata
    # datasets, so their dataset-index dates change at this level and nowhere lower.
    update_dataset_index_modified(
        ('vocabularies', 'vocabularies-versions'),
        date_issued, local_offset_from_utc
    )

    return aNewVocabulary, vocab_subpath, vocabularyUri, vocabularyVersionUri

# This function contains the last cell from the development Jupyter notebook simplified_process_rs_tdwg_org.ipynb
def update_standard_metadata(date_issued, local_offset_from_utc, standardUri, vocab_subpath, vocabularyUri, vocabularyVersionUri, aNewVocabulary):
    standards_table_filename = '../standards/standards.csv'
    standards_table = readCsv(standards_table_filename)

    standards_versions_joins_filename = '../standards/standards-versions.csv'
    standards_versions_joins = readCsv(standards_versions_joins_filename)

    standards_parts_filename = '../standards/standards-parts.csv'
    standards_parts = readCsv(standards_parts_filename)

    standards_versions_metadata_filename = '../standards-versions/standards-versions.csv'
    standards_versions_metadata = readCsv(standards_versions_metadata_filename)

    standards_versions_parts_filename = '../standards-versions/standards-versions-parts.csv'
    standards_versions_parts = readCsv(standards_versions_parts_filename)

    standards_versions_replacements_filename = '../standards-versions/standards-versions-replacements.csv'
    standards_versions_replacements = readCsv(standards_versions_replacements_filename)

    # Resolve vocabulary-version IRIs through vocabulary-version metadata.
    # This replaces URI-shape tests that attempted to distinguish vocabulary
    # versions from document versions.
    vocabularies_versions_metadata = readCsv('../vocabularies-versions/vocabularies-versions.csv')
    vv_version_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'version')[1]
    vv_vocabulary_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'vocabulary')[1]
    vocabulary_uri_by_version_uri = {
        row[vv_version_column]: row[vv_vocabulary_column]
        for row in vocabularies_versions_metadata[1:]
    }

    # generate the standard version URI
    standardVersionUri = standardUri + '/version/' + date_issued

    # check for the case where the script was previously run to update a different term list in the same new standard version
    temp = findColumnWithHeader(standards_versions_metadata[0], 'version')[1]
    alreadyAddedStandard = False
    for versionRow in standards_versions_metadata:
        if versionRow[temp] == standardVersionUri:
            alreadyAddedStandard = True

    modified_datetime = findColumnWithHeader(standards_table[0], 'document_modified')[1]
    standard_uri = findColumnWithHeader(standards_table[0], 'standard')[1]
    standard_label = findColumnWithHeader(standards_table[0], 'label')[1]
    standard_description = findColumnWithHeader(standards_table[0], 'description')[1]
    standard_created = findColumnWithHeader(standards_table[0], 'standard_created')[1]
    standard_modified = findColumnWithHeader(standards_table[0], 'standard_modified')[1]

    aNewStandard = True
    for rowNumber in range(1, len(standards_table)):
        if standardUri == standards_table[rowNumber][standard_uri]:
            aNewStandard = False
            standard_rowNumber = rowNumber
            # in cases where changes are made to a second term list of a new standard, the new modified date will be the same as before
            standards_table[rowNumber][standard_modified] = date_issued
            standards_table[rowNumber][modified_datetime] = isoTime(local_offset_from_utc)

            # Update the standard_label and standard_description columns from the standard configuration file, if they exist.
            if 'standard_label' in config_vocab:
                standards_table[rowNumber][standard_label] = config_vocab['standard_label']
            if 'standard_description' in config_vocab:
                standards_table[rowNumber][standard_description] = config_vocab['standard_description']

    if aNewStandard: # this will happen if the standard did not previously exist
        pass
        """
        try:
            new_standard_row = readCsv('files_for_new/new_standard.csv')[1]
        except:
            print('The standard was not found and there was no new_standard.csv file.')
            sys.exit()
        new_standard_row[standard_created] = date_issued
        new_standard_row[standard_modified] = date_issued
        new_standard_row[modified_datetime] = isoTime(local_offset_from_utc)
        # the row is set to what the last row will be after appending
        standard_rowNumber = len(standards_table)
        standards_table.append(new_standard_row)
        """
        # Create a new row for the standard table that is a list with length equal to the 0th row of the table
        new_standard_row = [''] * len(standards_table[0])

        new_standard_row[modified_datetime] = isoTime(local_offset_from_utc)

        # Assign the standard URI to the new standard row
        new_standard_row[standard_uri] = standardUri

        # Assign the standard label from the vocab configuration file to the new standard row
        new_standard_row[standard_label] = config_vocab['standard_label']

        # Assign the standard description from the vocab configuration file to the new standard row
        new_standard_row[standard_description] = config_vocab['standard_description']

        # Assign the created and modified dates to the new standard row
        new_standard_row[standard_created] = date_issued
        new_standard_row[standard_modified] = date_issued

        # Append the new standard row to the table
        standards_table.append(new_standard_row)

    writeCsv('../standards/standards.csv', standards_table)

    if not alreadyAddedStandard:
        standards_versions_joins.append([standardVersionUri, standardUri])
        writeCsv('../standards/standards-versions.csv', standards_versions_joins)

    # The unversioned parts table records current containment independently of
    # whether the Vocabulary itself is new in this release. Ensure that the
    # Standard -> Vocabulary relationship exists exactly once.
    matching_standard_parts = [
        row_number
        for row_number in range(1, len(standards_parts))
        if (standards_parts[row_number][0] == standardUri and
            standards_parts[row_number][1] == vocabularyUri)
    ]
    if len(matching_standard_parts) > 1:
        raise ValueError(
            'Standard ' + standardUri + ' contains duplicate unversioned part '
            'for Vocabulary ' + vocabularyUri + '.'
        )
    if matching_standard_parts:
        existing_part_row = standards_parts[matching_standard_parts[0]]
        if len(existing_part_row) < 3 or existing_part_row[2] != 'tdwgutility:Vocabulary':
            raise ValueError(
                'Standard ' + standardUri + ' already contains part ' +
                vocabularyUri + ' with a type other than tdwgutility:Vocabulary.'
            )
    else:
        standards_parts.append(
            [standardUri, vocabularyUri, 'tdwgutility:Vocabulary']
        )
    writeCsv('../standards/standards-parts.csv', standards_parts)

    # find the columns than contain needed information
    document_modified = findColumnWithHeader(standards_versions_metadata[0], 'document_modified')[1]
    version_uri = findColumnWithHeader(standards_versions_metadata[0], 'version')[1]
    version_issued = findColumnWithHeader(standards_versions_metadata[0], 'version_issued')[1]
    status_column = findColumnWithHeader(standards_versions_metadata[0], 'standard_status')[1]
    label_column = findColumnWithHeader(standards_versions_metadata[0], 'label')[1]
    description_column = findColumnWithHeader(standards_versions_metadata[0], 'description')[1]
    standard_uri = findColumnWithHeader(standards_versions_metadata[0], 'standard')[1]

    if not alreadyAddedStandard:
        # Create a new empty row for the standards versions table that is a list with length equal to the 0th row of the table
        newStandardRow = [''] * len(standards_versions_metadata[0])

        if aNewStandard: # this will happen if the standard did not previously exist
            pass
            """
            try:
                newStandardRow = readCsv('files_for_new/new_standard_version.csv')[1]
            except:
                print('The standard version was not found and there was no new_standard_version.csv file.')
                sys.exit()
            # the new row will be added to the end and therefore will have an index number - number of rows before appending
            mostRecentStandardNumber = len(standards_versions_metadata)
            """
        else:
            # Find the latest previous version by its explicit issued date.
            mostRecentStandardNumber = find_predecessor_row(
                standards_versions_metadata, standard_uri, standardUri,
                version_issued, date_issued, 'standard ' + standardUri
            )

            # change the status of the most recent standard to superseded
            standards_versions_metadata[mostRecentStandardNumber][status_column] = 'superseded'
            standards_versions_metadata[mostRecentStandardNumber][document_modified] = isoTime(local_offset_from_utc)

            # start the new standard row with the metadata from the most recent vocabulary
            #newStandardRow = copy.deepcopy(standards_versions_metadata[mostRecentStandardNumber])

        # substitute metadata to make the most recent standard version have the modified dates for the new standard version
        newStandardRow[document_modified] = isoTime(local_offset_from_utc)
        newStandardRow[version_uri] = standardVersionUri
        newStandardRow[version_issued] = date_issued
        newStandardRow[status_column] = 'recommended'
        # Replace the standard label and description if a new one is provided. Otherwise, use the previous one.
        if 'standard_label' in config_vocab:
            newStandardRow[label_column] = config_vocab['standard_label']
        else:
            newStandardRow[label_column] = standards_versions_metadata[mostRecentStandardNumber][label_column]
        if 'standard_description' in config_vocab:
            newStandardRow[description_column] = config_vocab['standard_description']
        else:
            newStandardRow[description_column] = standards_versions_metadata[mostRecentStandardNumber][description_column]
        newStandardRow[standard_uri] = standardUri

        # append the new term list row to the old list of term lists
        standards_versions_metadata.append(newStandardRow)

        # save as a file
        writeCsv('../standards-versions/standards-versions.csv', standards_versions_metadata)

    # Finding #29: construct the target Standard version as a complete snapshot
    # of the Standard's current declared parts. Resolve each current resource
    # identity through its own version metadata; do not inherit predecessor
    # omissions or retired parts.
    # Resolve Document identity -> version through the explicit join table, then
    # use version metadata only to determine which joined version applies on the
    # target date. This mirrors the repository model instead of assuming that
    # docs-versions.csv itself is the authoritative containment relation.
    def required_column(table, column_name, filename):
        found, column = findColumnWithHeader(table[0], column_name)
        if not found:
            raise ValueError(
                filename + ' lacks required column ' + column_name + '.'
            )
        return column

    documents_versions_joins_filename = '../docs/docs-versions.csv'
    documents_versions_joins = readCsv(documents_versions_joins_filename)
    dvj_document_column = required_column(
        documents_versions_joins, 'current_iri', documents_versions_joins_filename
    )
    dvj_version_column = required_column(
        documents_versions_joins, 'version_iri', documents_versions_joins_filename
    )

    document_versions_metadata_filename = '../docs-versions/docs-versions.csv'
    document_versions_metadata = readCsv(document_versions_metadata_filename)
    dv_document_column = required_column(
        document_versions_metadata, 'current_iri', document_versions_metadata_filename
    )
    dv_version_column = required_column(
        document_versions_metadata, 'version_iri', document_versions_metadata_filename
    )
    dv_date_column = required_column(
        document_versions_metadata, 'version_issued', document_versions_metadata_filename
    )

    document_metadata_by_version = {}
    for row in document_versions_metadata[1:]:
        version_value = row[dv_version_column]
        if version_value in document_metadata_by_version:
            raise ValueError(
                document_versions_metadata_filename + ' contains duplicate version_iri ' +
                version_value + '.'
            )
        document_metadata_by_version[version_value] = row

    vv_date_column = findColumnWithHeader(vocabularies_versions_metadata[0], 'version_issued')[1]
    target_date = datetime.date.fromisoformat(date_issued)

    declared_parts = [
        (row[1], row[2] if len(row) > 2 else '')
        for row in standards_parts[1:]
        if row[0] == standardUri
    ]
    declared_part_uris = [part_uri for part_uri, part_type in declared_parts]
    duplicate_declared_parts = sorted({
        identity for identity in declared_part_uris
        if declared_part_uris.count(identity) > 1
    })
    if duplicate_declared_parts:
        raise ValueError(
            'Standard ' + standardUri + ' contains duplicate current part(s): ' +
            ', '.join(duplicate_declared_parts) + '.'
        )

    def latest_version_on_or_before(metadata, identity_column, identity_value,
                                    date_column, version_column, context):
        candidates = []
        for row in metadata[1:]:
            if row[identity_column] != identity_value:
                continue
            try:
                candidate_date = datetime.date.fromisoformat(row[date_column])
            except (TypeError, ValueError):
                raise ValueError(
                    context + ' has invalid version date ' + repr(row[date_column]) + '.'
                )
            if candidate_date <= target_date:
                candidates.append((candidate_date, row[version_column]))
        if not candidates:
            raise ValueError(
                'Standard ' + standardUri + ' declares ' + context +
                ', but no version exists on or before ' + date_issued + '.'
            )
        latest_date = max(candidate[0] for candidate in candidates)
        latest = [uri for candidate_date, uri in candidates if candidate_date == latest_date]
        if len(latest) != 1:
            raise ValueError(
                context + ' has ' + str(len(latest)) + ' versions dated ' +
                latest_date.isoformat() + '.'
            )
        return latest[0]

    resolved_standard_parts = []
    resolved_part_identities = []
    for part_uri, part_type in declared_parts:
        if part_type == 'tdwgutility:Vocabulary':
            resolved_uri = latest_version_on_or_before(
                vocabularies_versions_metadata, vv_vocabulary_column, part_uri,
                vv_date_column, vv_version_column, 'Vocabulary ' + part_uri
            )
        elif part_type == 'foaf:Document':
            joined_versions = [
                row[dvj_version_column]
                for row in documents_versions_joins[1:]
                if row[dvj_document_column] == part_uri
            ]
            if not joined_versions:
                raise ValueError(
                    'Standard ' + standardUri + ' declares Document ' + part_uri +
                    ', but ../docs/docs-versions.csv contains no version relationship.'
                )

            candidates = []
            for joined_version in joined_versions:
                if joined_version not in document_metadata_by_version:
                    raise ValueError(
                        'Document ' + part_uri + ' references version ' + joined_version +
                        ' in ../docs/docs-versions.csv, but that version has no metadata '
                        'row in ../docs-versions/docs-versions.csv.'
                    )
                metadata_row = document_metadata_by_version[joined_version]
                try:
                    candidate_date = datetime.date.fromisoformat(
                        metadata_row[dv_date_column]
                    )
                except (TypeError, ValueError):
                    raise ValueError(
                        'Document ' + part_uri + ' version ' + joined_version +
                        ' has invalid version date ' +
                        repr(metadata_row[dv_date_column]) + '.'
                    )
                if candidate_date <= target_date:
                    candidates.append((candidate_date, joined_version))

            if not candidates:
                raise ValueError(
                    'Standard ' + standardUri + ' declares Document ' + part_uri +
                    ', but no joined version exists on or before ' + date_issued + '.'
                )
            latest_date = max(candidate[0] for candidate in candidates)
            latest = [
                uri for candidate_date, uri in candidates
                if candidate_date == latest_date
            ]
            if len(latest) != 1:
                raise ValueError(
                    'Document ' + part_uri + ' has ' + str(len(latest)) +
                    ' joined versions dated ' + latest_date.isoformat() + '.'
                )
            resolved_uri = latest[0]
        else:
            raise ValueError(
                'Standard ' + standardUri + ' declares part ' + part_uri +
                ' with unsupported resource type ' + repr(part_type) +
                '; cannot resolve its version generically.'
            )
        resolved_standard_parts.append(resolved_uri)
        resolved_part_identities.append(part_uri)

    if resolved_part_identities != declared_part_uris:
        raise ValueError(
            'Generated Standard version ' + standardVersionUri +
            ' does not exactly represent current Standard membership.'
        )

    # Replace the target-release snapshot wholesale on every pass. This makes
    # multi-namespace processing and same-release reruns converge to the same
    # complete state as current membership evolves during the run.
    standards_versions_parts = [standards_versions_parts[0]] + [
        row for row in standards_versions_parts[1:]
        if row[0] != standardVersionUri
    ]
    for part_version_uri in resolved_standard_parts:
        standards_versions_parts.append([standardVersionUri, part_version_uri])

    writeCsv('../standards-versions/standards-versions-parts.csv', standards_versions_parts)

    if not(aNewStandard) and not(alreadyAddedStandard):
        standards_versions_replacements.append([standardVersionUri, standards_versions_metadata[mostRecentStandardNumber][version_uri]])
        writeCsv('../standards-versions/standards-versions-replacements.csv', standards_versions_replacements)

    # This function changes the Standard and Standard Version metadata
    # datasets, so their dataset-index dates change at this level and nowhere lower.
    update_dataset_index_modified(
        ('standards', 'standards-versions'),
        date_issued, local_offset_from_utc
    )

def _preflight_read_csv(filename, errors):
    """Read and minimally validate a CSV used by this release."""
    if not os.path.isfile(filename):
        errors.append('required CSV does not exist: ' + filename)
        return None
    try:
        rows = readCsv(filename)
    except Exception as error:
        errors.append('could not read CSV ' + filename + ': ' + str(error))
        return None
    if not rows:
        errors.append('CSV is empty: ' + filename)
        return None
    header = rows[0]
    if not header or not any(value.strip() for value in header):
        errors.append('CSV has no usable header: ' + filename)
        return None
    duplicate_headers = sorted({value for value in header if header.count(value) > 1})
    if duplicate_headers:
        errors.append('CSV has duplicate column headers in ' + filename + ': ' + ', '.join(duplicate_headers))
    return rows


class PreflightValidationError(Exception):
    """Expected release-input validation failure."""
    pass


def _preflight_validate_unique_column(filename, column_name, errors):
    """Validate that a current-resource identity column contains no duplicates."""
    table = _preflight_read_csv(filename, errors)
    if table is None:
        return

    header = table[0]
    if column_name not in header:
        errors.append('CSV lacks ' + column_name + ' column: ' + filename)
        return

    column = header.index(column_name)
    values = [
        row[column]
        for row in table[1:]
        if len(row) > column
    ]
    duplicate_values = sorted({
        value for value in values
        if values.count(value) > 1
    })
    if duplicate_values:
        errors.append(
            filename + ' has duplicate ' + column_name + ' values: ' +
            ', '.join(duplicate_values)
        )


def _preflight_validate_unique_columns(filename, column_names, errors, table=None):
    """Validate that a composite metadata identity contains no duplicates."""
    if table is None:
        table = _preflight_read_csv(filename, errors)
    if table is None:
        return

    header = table[0]
    missing_columns = [column for column in column_names if column not in header]
    if missing_columns:
        errors.append(
            'CSV lacks column(s) required for uniqueness check in ' + filename + ': ' +
            ', '.join(missing_columns)
        )
        return

    indexes = [header.index(column) for column in column_names]
    values = []
    for row in table[1:]:
        if all(len(row) > index for index in indexes):
            values.append(tuple(row[index] for index in indexes))

    duplicate_values = sorted({
        value for value in values
        if values.count(value) > 1
    })
    if duplicate_values:
        errors.append(
            filename + ' has duplicate (' + ', '.join(column_names) + ') values: ' +
            '; '.join('(' + ', '.join(value) + ')' for value in duplicate_values)
        )


def _preflight_validate_date_column(filename, column_name, errors):
    """Validate canonical YYYY-MM-DD values in a historical metadata column."""
    table = _preflight_read_csv(filename, errors)
    if table is None:
        return

    header = table[0]
    if column_name not in header:
        errors.append('CSV lacks ' + column_name + ' column: ' + filename)
        return

    column = header.index(column_name)
    for row_number, row in enumerate(table[1:], start=2):
        if len(row) <= column:
            errors.append(
                filename + ' row ' + str(row_number) + ' has no value for ' +
                column_name
            )
            continue

        value = row[column]
        try:
            parsed_date = datetime.date.fromisoformat(value)
            if parsed_date.isoformat() != value:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(
                filename + ' row ' + str(row_number) + ' has invalid ' +
                column_name + ' date ' + repr(value) +
                '; expected YYYY-MM-DD'
            )


def preflight_validate_release():
    """Validate release configuration and inputs before metadata processing begins."""
    errors = []

    # Release-level configuration.
    try:
        parsed_date = datetime.date.fromisoformat(str(date_issued))
        if parsed_date.isoformat() != str(date_issued):
            raise ValueError
    except ValueError:
        errors.append('date_issued must use YYYY-MM-DD format: ' + repr(date_issued))

    if not re.fullmatch(r'[+-](?:0\d|1\d|2[0-3]):[0-5]\d', str(local_offset_from_utc)):
        errors.append('local_offset_from_utc must use +/-HH:MM format: ' + repr(local_offset_from_utc))

    if vocab_type not in (1, 2, 3):
        errors.append('vocab_type must be 1, 2, or 3: ' + repr(vocab_type))

    if not isinstance(revision_directory, str) or not revision_directory.strip():
        errors.append(
            'revision_directory must be a non-empty string: ' +
            repr(revision_directory)
        )

    # Current Vocabulary and Standard metadata represent singular resources.
    # Duplicate resource identities would make later update logic ambiguous.
    for current_filename, identity_column in (
        ('../vocabularies/vocabularies.csv', 'vocabulary'),
        ('../standards/standards.csv', 'standard'),
    ):
        _preflight_validate_unique_column(
            current_filename, identity_column, errors
        )

    # Historical dates drive release-state classification, predecessor selection,
    # and final status reconciliation. Each version IRI must identify exactly one
    # metadata row, and a resource may have at most one version on any given date.
    for historical_filename, resource_column, historical_date_column in (
        ('../term-lists-versions/term-lists-versions.csv', 'list', 'version_modified'),
        ('../vocabularies-versions/vocabularies-versions.csv', 'vocabulary', 'version_issued'),
        ('../standards-versions/standards-versions.csv', 'standard', 'version_issued'),
    ):
        _preflight_validate_date_column(
            historical_filename, historical_date_column, errors
        )
        _preflight_validate_unique_column(
            historical_filename, 'version', errors
        )
        _preflight_validate_unique_columns(
            historical_filename, (resource_column, historical_date_column), errors
        )

    if not isinstance(namespaces, list) or not namespaces:
        errors.append('namespaces must be a non-empty YAML list')
        namespace_items = []
    else:
        namespace_items = namespaces

    if not os.path.isdir(release_directory):
        errors.append('release revision directory does not exist: ' + release_directory)

    required_namespace_keys = {
        'namespace_uri', 'pref_namespace_prefix', 'database', 'borrowed',
        'new_term_list', 'utility_namespace', 'termlist_uri', 'label',
        'description', 'prepend_url', 'use_namespace_in_fragment', 'separator',
    }
    seen_prefixes = {}
    seen_databases = {}
    seen_namespace_uris = {}
    seen_term_lists = {}

    # new_term_list is a declaration about repository state, not an independent
    # source of truth. Validate it against the authoritative Term List metadata
    # before any namespace processing can create or overwrite infrastructure.
    term_lists_filename = '../term-lists/term-lists.csv'
    term_lists_table = _preflight_read_csv(term_lists_filename, errors)
    existing_term_list_iris = None
    if term_lists_table is not None:
        if 'list' not in term_lists_table[0]:
            errors.append('term-lists CSV lacks list column: ' + term_lists_filename)
        else:
            list_column = term_lists_table[0].index('list')
            term_list_iris = [
                row[list_column]
                for row in term_lists_table[1:]
                if len(row) > list_column
            ]
            duplicate_term_list_iris = sorted({
                iri for iri in term_list_iris
                if term_list_iris.count(iri) > 1
            })
            if duplicate_term_list_iris:
                errors.append(
                    'term-lists CSV has duplicate list values in ' +
                    term_lists_filename + ': ' +
                    ', '.join(duplicate_term_list_iris)
                )
            existing_term_list_iris = set(term_list_iris)

    for index, namespace_config in enumerate(namespace_items, start=1):
        context = 'namespaces[' + str(index) + ']'
        if not isinstance(namespace_config, dict):
            errors.append(context + ' must be a YAML mapping')
            continue

        missing = sorted(required_namespace_keys - set(namespace_config))
        if missing:
            errors.append(context + ' is missing required keys: ' + ', '.join(missing))
            continue

        prefix = namespace_config['pref_namespace_prefix']
        database_name = namespace_config['database']
        namespace_uri = namespace_config['namespace_uri']
        borrowed = namespace_config['borrowed']
        new_term_list = namespace_config['new_term_list']
        utility_namespace = namespace_config['utility_namespace']
        configured_termlist_uri = namespace_config['termlist_uri']
        effective_termlist_uri = configured_termlist_uri or namespace_uri

        for field_name in ('borrowed', 'new_term_list', 'utility_namespace', 'use_namespace_in_fragment'):
            if not isinstance(namespace_config[field_name], bool):
                errors.append(context + '.' + field_name + ' must be true or false')

        for field_name in ('namespace_uri', 'pref_namespace_prefix', 'database', 'label', 'description', 'prepend_url'):
            value = namespace_config[field_name]
            if not isinstance(value, str) or not value.strip():
                errors.append(context + '.' + field_name + ' must be a non-empty string')

        if not isinstance(configured_termlist_uri, str):
            errors.append(context + '.termlist_uri must be a string (empty is allowed)')

        if isinstance(database_name, str):
            if any(character.isspace() for character in database_name):
                errors.append(context + '.database must not contain whitespace: ' + repr(database_name))
            if database_name.endswith('-versions'):
                errors.append(context + '.database must not end with -versions: ' + repr(database_name))

        for value, seen, label in (
            (prefix, seen_prefixes, 'pref_namespace_prefix'),
            (database_name, seen_databases, 'database'),
            (namespace_uri, seen_namespace_uris, 'namespace_uri'),
            (effective_termlist_uri, seen_term_lists, 'effective termlist_uri'),
        ):
            try:
                duplicate = value in seen
            except TypeError:
                continue
            if duplicate:
                errors.append(context + ' duplicates ' + label + ' from ' + seen[value] + ': ' + repr(value))
            else:
                seen[value] = context

        # Preserve the existing borrowed/non-borrowed contract without adding
        # new URI-pattern policy here; URI construction is a separate finding.
        if borrowed is True and configured_termlist_uri == '':
            errors.append(context + '.termlist_uri must be supplied for a borrowed namespace')
        if borrowed is False and configured_termlist_uri not in ('', namespace_uri):
            errors.append(context + '.termlist_uri must be empty or equal namespace_uri for a non-borrowed namespace')

        # The configured new_term_list flag must agree with repository state.
        # This prevents the setup path from treating an existing Term List as new
        # or skipping required setup for a Term List that is actually absent.
        if isinstance(new_term_list, bool) and existing_term_list_iris is not None:
            term_list_exists = effective_termlist_uri in existing_term_list_iris
            if new_term_list and term_list_exists:
                errors.append(
                    context + '.new_term_list is true, but the effective Term List IRI '
                    'already exists in ' + term_lists_filename + ': ' +
                    effective_termlist_uri
                )
            elif not new_term_list and not term_list_exists:
                errors.append(
                    context + '.new_term_list is false, but the effective Term List IRI '
                    'does not exist in ' + term_lists_filename + ': ' +
                    effective_termlist_uri
                )

        if not isinstance(prefix, str) or not prefix.strip():
            continue

        modifications_filename = os.path.join(release_directory, prefix + '.csv')
        modifications = _preflight_read_csv(modifications_filename, errors)
        if modifications is None:
            continue

        header = modifications[0]
        if 'term_localName' not in header:
            errors.append('modifications CSV lacks term_localName column: ' + modifications_filename)
            continue

        local_name_column = header.index('term_localName')
        local_names = []
        for row_number, row in enumerate(modifications[1:], start=2):
            if len(row) != len(header):
                errors.append(modifications_filename + ' row ' + str(row_number) + ' has ' + str(len(row)) + ' columns; expected ' + str(len(header)))
                continue
            local_name = row[local_name_column].strip()
            if not local_name:
                errors.append(modifications_filename + ' row ' + str(row_number) + ' has an empty term_localName')
            else:
                local_names.append(local_name)

        duplicate_local_names = sorted({name for name in local_names if local_names.count(name) > 1})
        if duplicate_local_names:
            errors.append('modifications CSV has duplicate term_localName values in ' + modifications_filename + ': ' + ', '.join(duplicate_local_names))

        # Existing term lists must already have the tables that processing reads.
        # New term lists are intentionally left to the established template path.
        if new_term_list is False and isinstance(database_name, str) and database_name:
            current_terms_filename = os.path.join('..', database_name, database_name + '.csv')
            current_terms = _preflight_read_csv(current_terms_filename, errors)
            if current_terms is not None:
                current_header = current_terms[0]
                if 'term_localName' not in current_header:
                    errors.append('current-terms CSV lacks term_localName column: ' + current_terms_filename)
                else:
                    current_local_name_column = current_header.index('term_localName')
                    current_local_names = [
                        row[current_local_name_column]
                        for row in current_terms[1:]
                        if len(row) > current_local_name_column
                    ]
                    duplicate_current_local_names = sorted({
                        name for name in current_local_names
                        if current_local_names.count(name) > 1
                    })
                    if duplicate_current_local_names:
                        errors.append(
                            'current-terms CSV has duplicate term_localName values in ' +
                            current_terms_filename + ': ' +
                            ', '.join(duplicate_current_local_names)
                        )
                missing_current_columns = sorted(set(header) - set(current_header))
                if missing_current_columns:
                    errors.append('modifications CSV contains columns absent from current-terms CSV ' + current_terms_filename + ': ' + ', '.join(missing_current_columns))

            if borrowed is False and utility_namespace is False:
                versions_name = database_name + '-versions'
                versions_filename = os.path.join('..', versions_name, versions_name + '.csv')
                versions_table = _preflight_read_csv(versions_filename, errors)
                if versions_table is not None:
                    versions_header = versions_table[0]
                    if 'term_localName' not in versions_header:
                        errors.append('term-versions CSV lacks term_localName column: ' + versions_filename)

                    if 'version' not in versions_header:
                        errors.append('term-versions CSV lacks version column: ' + versions_filename)
                    else:
                        version_column = versions_header.index('version')
                        version_iris = [
                            row[version_column]
                            for row in versions_table[1:]
                            if len(row) > version_column
                        ]
                        duplicate_version_iris = sorted({
                            version_iri for version_iri in version_iris
                            if version_iris.count(version_iri) > 1
                        })
                        if duplicate_version_iris:
                            errors.append(
                                versions_filename + ' has duplicate version values: ' +
                                ', '.join(duplicate_version_iris)
                            )

                    # A term may have at most one historical version for a given
                    # issue date. Otherwise release-state classification and
                    # predecessor chronology are ambiguous.
                    _preflight_validate_unique_columns(
                        versions_filename, ('term_localName', 'version_issued'),
                        errors, table=versions_table
                    )

                    missing_version_columns = sorted(set(header) - set(versions_header))
                    if missing_version_columns:
                        errors.append('modifications CSV contains columns absent from term-versions CSV ' + versions_filename + ': ' + ', '.join(missing_version_columns))

                    # Term-version dates are used both to classify same-release
                    # reruns and to select predecessors for modified terms.
                    if 'version_issued' not in versions_header:
                        errors.append('term-versions CSV lacks version_issued column: ' + versions_filename)
                    else:
                        version_issued_column = versions_header.index('version_issued')
                        for version_row_number, version_row in enumerate(
                                versions_table[1:], start=2):
                            if len(version_row) <= version_issued_column:
                                errors.append(
                                    versions_filename + ' row ' +
                                    str(version_row_number) +
                                    ' has no value for version_issued'
                                )
                                continue
                            issued_value = version_row[version_issued_column]
                            try:
                                parsed_issued = datetime.date.fromisoformat(issued_value)
                                if parsed_issued.isoformat() != issued_value:
                                    raise ValueError
                            except (TypeError, ValueError):
                                errors.append(
                                    versions_filename + ' row ' +
                                    str(version_row_number) +
                                    ' has invalid version_issued date ' +
                                    repr(issued_value) +
                                    '; expected YYYY-MM-DD'
                                )

    if errors:
        error_label = 'error' if len(errors) == 1 else 'errors'
        raise PreflightValidationError(
            'Preflight validation failed with ' + str(len(errors)) + ' ' + error_label + ':\n' +
            '\n'.join('  - ' + error for error in errors)
        )

    print('preflight validation passed')


try:
    preflight_validate_release()
except PreflightValidationError as error:
    print(str(error), file=sys.stderr)
    print('', file=sys.stderr)
    raise SystemExit(2)

# -----------------------
# Main routine
# -----------------------
        
# Finding #29: reconcile configured containment before processing individual
# namespaces. Membership itself is release state: an unchanged Term List may be
# incorporated into a Vocabulary, and that containment change must be visible to
# the complete target snapshot created later in the run.
def reconcile_configured_containment():
    vocabulary_members = readCsv('../vocabularies/vocabularies-members.csv')
    standard_parts = readCsv('../standards/standards-parts.csv')
    vocabulary_membership_changed = False
    standard_membership_changed = False

    configured_term_lists = []
    for namespace in namespaces:
        if namespace['utility_namespace']:
            continue
        term_list_uri = namespace['termlist_uri'] or namespace['namespace_uri']
        if term_list_uri not in configured_term_lists:
            configured_term_lists.append(term_list_uri)

    for term_list_uri in configured_term_lists:
        matches = [
            row for row in vocabulary_members[1:]
            if row[0] == vocabularyIri and row[1] == term_list_uri
        ]
        if len(matches) > 1:
            raise ValueError(
                'Vocabulary ' + vocabularyIri + ' contains duplicate current '
                'membership for Term List ' + term_list_uri + '.'
            )
        if not matches:
            vocabulary_members.append([vocabularyIri, term_list_uri])
            vocabulary_membership_changed = True

    vocabulary_part_matches = [
        row for row in standard_parts[1:]
        if row[0] == standardUri and row[1] == vocabularyIri
    ]
    if len(vocabulary_part_matches) > 1:
        raise ValueError(
            'Standard ' + standardUri + ' contains duplicate current part for '
            'Vocabulary ' + vocabularyIri + '.'
        )
    if vocabulary_part_matches:
        if (len(vocabulary_part_matches[0]) < 3 or
                vocabulary_part_matches[0][2] != 'tdwgutility:Vocabulary'):
            raise ValueError(
                'Standard ' + standardUri + ' contains part ' + vocabularyIri +
                ' with a type other than tdwgutility:Vocabulary.'
            )
    else:
        standard_parts.append([standardUri, vocabularyIri, 'tdwgutility:Vocabulary'])
        standard_membership_changed = True

    if vocabulary_membership_changed:
        writeCsv('../vocabularies/vocabularies-members.csv', vocabulary_members)
    if standard_membership_changed:
        writeCsv('../standards/standards-parts.csv', standard_parts)

    return vocabulary_membership_changed, standard_membership_changed


vocabulary_membership_changed, standard_membership_changed = reconcile_configured_containment()

# Set up a list to keep track of the IRIs of terms that have changed so that they can later be added to the
# Executive Committee decisions CSV
changed_terms_iris = []
higher_level_update_performed = False

for namespace in namespaces:
    # Step 1 (from first cell in development Jupyter notebook simplified_process_rs_tdwg_org.ipynb)
    # Set the values of flags that control the flow of program execution
    borrowed = namespace['borrowed']
    new_term_list = namespace['new_term_list']
    utility_namespace = namespace['utility_namespace']
    use_namespace_in_fragment = namespace['use_namespace_in_fragment']

    # Set the values of namespace-specific configuration variables
    namespaceUri = namespace['namespace_uri']
    database = namespace['database']
    prepend_url = namespace['prepend_url']
    separator = namespace['separator']
    versions = database + '-versions'
    # The modifications file location is derived from the release date and namespace prefix.
    # Every configured namespace is expected to have a CSV in this directory. A header-only
    # CSV means that the namespace participates in the release but has no term changes.
    modifications_filename = os.path.join(
        release_directory,
        namespace['pref_namespace_prefix'] + '.csv'
    )
    version_namespace = namespaceUri + 'version/'
    """
    if new_term_list:
        if 'label' in namespace:
            term_list_label = namespace['label']
        else:
            term_list_label = ''
        if 'description' in namespace:
            term_list_description = namespace['description']
        else:
            term_list_description = ''
        if 'pref_namespace_prefix' in namespace:
            pref_namespace_prefix = namespace['pref_namespace_prefix']
        else:
            pref_namespace_prefix = ''
    else:
        term_list_label = ''
        term_list_description = ''
        pref_namespace_prefix = ''
    """
    # No longer make it an option to provide these values in the namespace configuration file. They are now required.
    term_list_label = namespace['label']
    term_list_description = namespace['description']
    pref_namespace_prefix = namespace['pref_namespace_prefix']

    # For borrowed terms, the termlist_uri will differ from the namespace URI. 
    # For terms minted by TDWG that follow URI pattern conventions, this should be the empty string and 
    # the termlist_uri will be set to the namespace URI.
    # In both cases the termlist version URI will be constructed from the namespace URI
    termlist_uri = namespace['termlist_uri']

    if termlist_uri == '':
        termlist_uri = namespaceUri
    else:
        # Let users get away with specifying a termlist URI as long as it's the same as the TDWG-issued namespace
        if not borrowed and namespaceUri != termlist_uri:
            print('WARNING: TDWG-minted namespaces should not have a configuration value for termlist_uri!')
            print('Namespace URI =', namespaceUri)
            print('Term list URI =', termlist_uri)
            print()

    # Step 2. Create new mapping and configuration files. If run for existing term lists, it will overwrite a bunch of stuff
    if new_term_list:
        generate_and_copy_mapping_and_config_files(vocab_type, namespaceUri, database, modifications_filename)

    # Step 3. Determine values needed to interpret and modify tables later
    terms_metadata, modifications_metadata, mods_local_name, metadata_localname_column, mods_term_localName, new_terms, modified_terms = determine_state_of_data_tables(database, versions, borrowed, utility_namespace, modifications_filename, date_issued)

    # Add the IRIs of terms that have changed to the list of changed terms
    for term in modified_terms:
        changed_terms_iris.append(namespaceUri + term)
    for term in new_terms:
        changed_terms_iris.append(namespaceUri + term)

    # A namespace can participate in the vocabulary without having term changes in this release.
    # Header-only modifications CSV files therefore mean "carry forward the existing current terms".
    # In that case, do not mint a new term-list version, vocabulary version, or standard version
    # merely because the namespace is present in config.yaml.
    has_term_changes = bool(new_terms or modified_terms)

    if has_term_changes or new_term_list:
        # Step 4. Create term versions-related metadata. Generally only applies to TDWG-minted terms, not borrowed ones
        if not borrowed and not utility_namespace:
            generate_term_versions_metadata(database, versions, version_namespace, mods_local_name, modified_terms, local_offset_from_utc, date_issued, modifications_metadata)

        # Step 5. Generate current terms metadata
        version_uri, aNewTermList, term_lists_versions_members, term_lists_versions_metadata, mostRecentListNumber, termlistVersionUri, term_lists_versions_replacements, term_lists_table, term_list_rowNumber = generate_current_terms_metadata(standardUri, terms_metadata, modifications_metadata, mods_local_name, modified_terms, local_offset_from_utc, date_issued, namespaceUri, termlist_uri, database, versions, term_list_label, term_list_description, pref_namespace_prefix, use_namespace_in_fragment, prepend_url, separator, borrowed)

        # Step 6. Update list of termlist version members and add the termlist replacement (TDWG namespaces only)
        if not borrowed and not utility_namespace:
            update_termlist_version_members(aNewTermList, mostRecentListNumber, date_issued, namespaceUri, database, new_terms, modified_terms, version_uri, termlistVersionUri, term_lists_versions_metadata, term_lists_versions_members, term_lists_versions_replacements)

        # Step 7. Update vocabulary-related metadata
        if not utility_namespace: # utility namespaces are not part of any vocabularies or standards
            aNewVocabulary, vocab_subpath, vocabularyUri, vocabularyVersionUri = update_vocabulary_metadata(date_issued, local_offset_from_utc, term_lists_table, term_list_rowNumber, termlistVersionUri, vocabularyIri, aNewTermList, termlist_uri)

        # Step 8. Update standard-related metadata
        if not utility_namespace: # utility namespaces are not part of any vocabularies or standards
            update_standard_metadata(date_issued, local_offset_from_utc, standardUri, vocab_subpath, vocabularyUri, vocabularyVersionUri, aNewVocabulary)
            higher_level_update_performed = True
    else:
        print('no term changes for', namespaceUri, '- carrying forward existing current terms and versions')

    namespace_results.append({
        'prefix': pref_namespace_prefix,
        'namespace_uri': namespaceUri,
        'termlist_uri': termlist_uri,
        'term_list_identity': urllib.parse.urlsplit(termlist_uri).path.strip('/'),
        'input_file': modifications_filename,
        'new_terms': list(new_terms),
        'modified_terms': list(modified_terms),
        'new_term_list': new_term_list,
        'changed': has_term_changes or new_term_list
    })

    print('completed', namespaceUri, 'namespace')

# A containment-only release still requires new higher-level versions even when
# every configured namespace has a header-only modifications file. Use one
# existing configured Term List only as the entry point; the update functions
# rebuild the complete Vocabulary and Standard snapshots from authoritative
# current membership, so no special meaning is attached to the selected list.
if ((vocabulary_membership_changed or standard_membership_changed) and
        not higher_level_update_performed):
    candidate_term_lists = [
        (namespace['termlist_uri'] or namespace['namespace_uri'])
        for namespace in namespaces
        if not namespace['utility_namespace']
    ]
    if not candidate_term_lists:
        raise ValueError(
            'Containment changed, but no non-utility configured Term List is available '
            'to create the containing Vocabulary and Standard versions.'
        )

    selected_term_list_uri = candidate_term_lists[0]
    term_lists_table = readCsv('../term-lists/term-lists.csv')
    list_uri_column = findColumnWithHeader(term_lists_table[0], 'list')[1]
    selected_rows = [
        row_number for row_number in range(1, len(term_lists_table))
        if term_lists_table[row_number][list_uri_column] == selected_term_list_uri
    ]
    if len(selected_rows) != 1:
        raise ValueError(
            'Expected exactly one current Term List row for ' + selected_term_list_uri +
            '; found ' + str(len(selected_rows)) + '.'
        )
    selected_row = selected_rows[0]

    term_list_versions = readCsv('../term-lists-versions/term-lists-versions.csv')
    tlv_version = findColumnWithHeader(term_list_versions[0], 'version')[1]
    tlv_list = findColumnWithHeader(term_list_versions[0], 'list')[1]
    tlv_date = findColumnWithHeader(term_list_versions[0], 'version_modified')[1]
    target_date = datetime.date.fromisoformat(date_issued)
    candidates = []
    for row in term_list_versions[1:]:
        if row[tlv_list] != selected_term_list_uri:
            continue
        candidate_date = datetime.date.fromisoformat(row[tlv_date])
        if candidate_date <= target_date:
            candidates.append((candidate_date, row[tlv_version]))
    if not candidates:
        raise ValueError(
            'No version of Term List ' + selected_term_list_uri +
            ' exists on or before ' + date_issued + '.'
        )
    latest_date = max(candidate[0] for candidate in candidates)
    latest_versions = [uri for candidate_date, uri in candidates if candidate_date == latest_date]
    if len(latest_versions) != 1:
        raise ValueError(
            'Term List ' + selected_term_list_uri + ' has ' +
            str(len(latest_versions)) + ' versions dated ' + latest_date.isoformat() + '.'
        )

    aNewVocabulary, vocab_subpath, vocabularyUri, vocabularyVersionUri = update_vocabulary_metadata(
        date_issued, local_offset_from_utc, term_lists_table, selected_row,
        latest_versions[0], vocabularyIri, False, selected_term_list_uri
    )
    update_standard_metadata(
        date_issued, local_offset_from_utc, standardUri, vocab_subpath,
        vocabularyUri, vocabularyVersionUri, aNewVocabulary
    )

# -----------------------
# Reconcile version status for the target release.
#
# Per-resource processing changes predecessor status as a side effect of creating
# a target version. On a same-release rerun, the target version already exists,
# so reconstruct status from resource identity and release chronology instead of
# depending on the state present at the start of the run.
# -----------------------
def reconcile_version_statuses(filename, identity_column_name, date_column_name,
                               status_column_name, date_issued, local_offset_from_utc):
    table = readCsv(filename)
    target_date = datetime.datetime.strptime(date_issued, '%Y-%m-%d').date()

    document_modified_column = findColumnWithHeader(table[0], 'document_modified')[1]
    identity_column = findColumnWithHeader(table[0], identity_column_name)[1]
    date_column = findColumnWithHeader(table[0], date_column_name)[1]
    status_column = findColumnWithHeader(table[0], status_column_name)[1]

    # Only resources for which this release actually has a version participate.
    target_identities = set()
    parsed_dates = {}
    for row_number in range(1, len(table)):
        version_date = datetime.datetime.strptime(
            table[row_number][date_column], '%Y-%m-%d'
        ).date()
        parsed_dates[row_number] = version_date
        if version_date == target_date:
            target_identities.add(table[row_number][identity_column])

    changed = False
    for row_number in range(1, len(table)):
        row = table[row_number]
        if row[identity_column] not in target_identities:
            continue

        version_date = parsed_dates[row_number]
        if version_date == target_date:
            desired_status = 'recommended'
        elif version_date < target_date:
            desired_status = 'superseded'
        else:
            # Do not alter a later release if processing an older release state.
            continue

        if row[status_column] != desired_status:
            row[status_column] = desired_status
            row[document_modified_column] = isoTime(local_offset_from_utc)
            changed = True

    writeCsv(filename, table)
    return changed


if reconcile_version_statuses(
    '../term-lists-versions/term-lists-versions.csv',
    'list', 'version_modified', 'status',
    date_issued, local_offset_from_utc
):
    update_dataset_index_modified(
        ('term-lists-versions',), date_issued, local_offset_from_utc
    )

if reconcile_version_statuses(
    '../vocabularies-versions/vocabularies-versions.csv',
    'vocabulary', 'version_issued', 'vocabulary_status',
    date_issued, local_offset_from_utc
):
    update_dataset_index_modified(
        ('vocabularies-versions',), date_issued, local_offset_from_utc
    )

if reconcile_version_statuses(
    '../standards-versions/standards-versions.csv',
    'standard', 'version_issued', 'standard_status',
    date_issued, local_offset_from_utc
):
    update_dataset_index_modified(
        ('standards-versions',), date_issued, local_offset_from_utc
    )

# -----------------------
# Once the namespace loop is complete, values in the general_configuration
# files must be updated using values from the config.yaml file.
# -----------------------

# Read the text of the general_configuration.yaml file.
with open('document_metadata_processing/general_configuration.yaml', 'rt') as file_object:
    general_config_text = file_object.read()

# Replace versionDate value with the new date_issued value.
general_config_text = re.sub('versionDate:.*\n', "versionDate: '" + date_issued + "'\n", general_config_text)

# Replace the utcOfset value with the new local_offset_from_utc value.
general_config_text = re.sub('utcOffset:.*\n', "utcOffset: " + local_offset_from_utc + "\n", general_config_text)

# Replace the docIri with the new list_of_terms_iri value. Assumes "docIri" is at the beginning of the line in the yaml file. Commented docIri's are unaffected.
general_config_text = re.sub(
    r'^(docIri:\s*).*$',
    r'\1' + config['list_of_terms_iri'],
    general_config_text,
    flags=re.MULTILINE
)

# Write the updated text to the file.
with open('document_metadata_processing/general_configuration.yaml', 'wt') as file_object:
    file_object.write(general_config_text)

# -----------------------
# Update the Executive Committee decisions metadata
# -----------------------
    
# Open and read in the Executive Committee decisions CSV file with all values as strings
decisions_df = pd.read_csv('../decisions/decisions.csv', dtype=str)

# The decision identity is authoritative configuration, not something inferred from
# the ordering or contents of existing decision metadata.
decision_number_string = str(config['decision_number'])
decision_local_name = 'decision-' + date_issued + '_' + decision_number_string
decision_label = 'TDWG Executive Committee decision ' + decision_number_string

# Determine whether this exact decision identity already exists.  On reruns, reuse
# it.  If the identity exists with conflicting metadata, abort rather than silently
# reusing or overwriting a different decision.
matching_decisions = decisions_df[decisions_df['term_localName'] == decision_local_name]

if len(matching_decisions) > 1:
    raise ValueError(
        'Executive Committee decision identity occurs more than once in decisions.csv: ' +
        decision_local_name
    )

if len(matching_decisions) == 1:
    existing_decision = matching_decisions.iloc[0]
    conflicts = []
    if existing_decision['label'] != decision_label:
        conflicts.append(
            "label is '" + str(existing_decision['label']) +
            "' but config implies '" + decision_label + "'"
        )
    if existing_decision['rdfs_comment'] != config['decisions_text']:
        conflicts.append('rdfs_comment does not match decisions_text in config.yaml')

    if conflicts:
        raise ValueError(
            'Executive Committee decision identity conflicts with existing metadata for ' +
            decision_local_name + ': ' + '; '.join(conflicts)
        )
else:
    # Add the configured decision to the decisions CSV file.
    row_dict = {}
    row_dict['document_modified'] = isoTime(local_offset_from_utc)
    row_dict['term_localName'] = decision_local_name
    row_dict['term_isDefinedBy'] = 'http://rs.tdwg.org/decisions/'
    row_dict['term_modified'] = date_issued
    row_dict['label'] = decision_label
    row_dict['rdfs_comment'] = config['decisions_text']
    decisions_df = pd.concat([decisions_df, pd.DataFrame([row_dict])], ignore_index=True)

    # Write the updated decisions CSV file.
    decisions_df.to_csv('../decisions/decisions.csv', index=False)

# For each term that has changed, add the IRI and decision term_localName as a row to the decisions-links.csv file.

# Open and read in the decisions-links CSV file with all values as strings
decisions_links_df = pd.read_csv('../decisions/decisions-links.csv', dtype=str)

# Add a row to the decisions-links CSV file for each term that has changed
new_decision_link_rows = []

for term_iri in changed_terms_iris:
    row_dict = {}
    row_dict['linked_affected_resource'] = term_iri
    row_dict['decision_localName'] = decision_local_name
    new_decision_link_rows.append(row_dict)

decisions_links_df = pd.concat(
    [decisions_links_df, pd.DataFrame(new_decision_link_rows)],
    ignore_index=True
)
# Write the updated decisions-links CSV file
decisions_links_df = decisions_links_df.drop_duplicates(keep='first')
# A relationship is a set member; reruns must not append it again.
decisions_links_df.to_csv('../decisions/decisions-links.csv', index=False)

# -----------------------
# Update the human-readable document metadata associated with this release.
# The general configuration file was prepared above from config.yaml.
# -----------------------

process_dir = os.path.dirname(os.path.abspath(__file__))
update_document_metadata(
    repo_path=os.path.abspath(os.path.join(process_dir, '..')),
    general_config_path=os.path.join(
        process_dir,
        'document_metadata_processing',
        'general_configuration.yaml'
    )
)

# -----------------------
# Generate a GitHub-release-ready summary of the proposed release.
# This is intentionally a resource/list-level report; individual term changes
# belong in the processing log and other detailed change documentation.
# -----------------------

def _column_index(header, name, required=True):
    if name in header:
        return header.index(name)
    if required:
        raise ValueError('Release report could not find required column: ' + name)
    return None


def _rows_by_value(rows, column_name, value):
    column = _column_index(rows[0], column_name)
    return [row for row in rows[1:] if row[column] == value]


def _replacement_predecessor(filename, current_version):
    rows = readCsv(filename)
    matches = [row[1] for row in rows[1:] if len(row) >= 2 and row[0] == current_version]
    if len(matches) > 1:
        raise ValueError(
            'Release report found more than one predecessor for ' + current_version +
            ' in ' + filename
        )
    return matches[0] if matches else None


def _version_display_date(version_uri, metadata_rows, date_column):
    version_column = _column_index(metadata_rows[0], 'version')
    date_index = _column_index(metadata_rows[0], date_column)
    matches = [row[date_index] for row in metadata_rows[1:] if row[version_column] == version_uri]
    return matches[0] if len(matches) == 1 else version_uri


def _load_all_version_labels_and_replacements():
    labels = {}
    replacements = {}
    for metadata_path in sorted(Path('..').glob('*-versions/*-versions.csv')):
        rows = readCsv(str(metadata_path))
        if not rows or 'version' not in rows[0]:
            continue
        version_index = rows[0].index('version')
        label_index = rows[0].index('label') if 'label' in rows[0] else None
        for row in rows[1:]:
            if version_index >= len(row) or not row[version_index]:
                continue
            label = row[label_index] if label_index is not None and label_index < len(row) else ''
            if label:
                labels[row[version_index]] = label
    for replacement_path in sorted(Path('..').glob('*-versions/*-versions-replacements.csv')):
        rows = readCsv(str(replacement_path))
        for row in rows[1:]:
            if len(row) >= 2 and row[0] and row[1]:
                replacements[row[0]] = row[1]
    return labels, replacements


def generate_release_report():
    vocabulary_versions = readCsv('../vocabularies-versions/vocabularies-versions.csv')
    vocabulary_members = readCsv('../vocabularies-versions/vocabularies-versions-members.csv')
    term_list_versions = readCsv('../term-lists-versions/term-lists-versions.csv')
    standard_versions = readCsv('../standards-versions/standards-versions.csv')
    standard_parts = readCsv('../standards-versions/standards-versions-parts.csv')

    vocabulary_path_parts = [
        part for part in urllib.parse.urlsplit(vocabularyIri).path.split('/') if part
    ]
    if not vocabulary_path_parts:
        raise ValueError('Configured vocabulary IRI has no path component: ' + vocabularyIri)

    current_vocabulary_version = (
        'http://rs.tdwg.org/version/' + vocabulary_path_parts[-1] + '/' + date_issued
    )
    current_standard_version = standardUri + '/version/' + date_issued

    if len(_rows_by_value(vocabulary_versions, 'version', current_vocabulary_version)) != 1:
        raise ValueError(
            'Release report expected exactly one Vocabulary version row for ' +
            current_vocabulary_version
        )
    if len(_rows_by_value(standard_versions, 'version', current_standard_version)) != 1:
        raise ValueError(
            'Release report expected exactly one Standard version row for ' +
            current_standard_version
        )

    previous_vocabulary_version = _replacement_predecessor(
        '../vocabularies-versions/vocabularies-versions-replacements.csv',
        current_vocabulary_version,
    )
    previous_standard_version = _replacement_predecessor(
        '../standards-versions/standards-versions-replacements.csv',
        current_standard_version,
    )

    tlv_version = _column_index(term_list_versions[0], 'version')
    tlv_identity = _column_index(term_list_versions[0], 'list_localName')
    tlv_date = _column_index(term_list_versions[0], 'version_modified')
    tlv_label = _column_index(term_list_versions[0], 'label', required=False)

    term_list_by_version = {}
    for row in term_list_versions[1:]:
        identity = row[tlv_identity].rstrip('/')
        label = row[tlv_label] if tlv_label is not None and row[tlv_label] else identity
        term_list_by_version[row[tlv_version]] = {
            'identity': identity,
            'date': row[tlv_date],
            'label': label,
        }

    def vocabulary_composition(version_uri):
        if not version_uri:
            return {}
        result = {}
        unresolved = []
        for row in vocabulary_members[1:]:
            if row[0] != version_uri:
                continue
            member_uri = row[1]
            metadata = term_list_by_version.get(member_uri)
            if metadata is None:
                unresolved.append(member_uri)
                continue
            identity = metadata['identity']
            if identity in result:
                raise ValueError(
                    'Release report found duplicate Term List identity ' + identity +
                    ' in Vocabulary version ' + version_uri
                )
            result[identity] = dict(metadata, version=member_uri)
        if unresolved:
            raise ValueError(
                'Release report could not resolve Term List version member(s) of ' +
                version_uri + ': ' + ', '.join(unresolved)
            )
        return result

    current_lists = vocabulary_composition(current_vocabulary_version)
    previous_lists = vocabulary_composition(previous_vocabulary_version)

    list_rows = []
    for identity in sorted(set(current_lists) | set(previous_lists)):
        current = current_lists.get(identity)
        previous = previous_lists.get(identity)
        if current and not previous:
            change = 'Added'
            display = current
        elif previous and not current:
            change = 'Removed'
            display = previous
        elif current['version'] != previous['version']:
            change = 'Updated'
            display = current
        else:
            change = 'Unchanged'
            display = current
        list_rows.append((identity, display, change, previous, current))

    labels_by_version, replacements_by_version = _load_all_version_labels_and_replacements()

    def standard_composition(version_uri):
        if not version_uri:
            return []
        return [row[1] for row in standard_parts[1:] if row[0] == version_uri]

    current_standard_parts = standard_composition(current_standard_version)
    previous_standard_parts = standard_composition(previous_standard_version)
    previous_remaining = list(previous_standard_parts)
    standard_rows = []

    for current_part in current_standard_parts:
        if current_part in previous_remaining:
            standard_rows.append((current_part, 'Unchanged', current_part))
            previous_remaining.remove(current_part)
            continue
        replaced = replacements_by_version.get(current_part)
        if replaced and replaced in previous_remaining:
            standard_rows.append((current_part, 'Updated', replaced))
            previous_remaining.remove(replaced)
        else:
            standard_rows.append((current_part, 'Added', None))
    for previous_part in previous_remaining:
        standard_rows.append((previous_part, 'Removed', previous_part))

    report_lines = []
    report_lines.append('# Darwin Core release ' + date_issued)
    report_lines.append('')
    report_lines.append(
        'This report summarizes this Darwin Core release at the Standard, Vocabulary, '
        'Term List, and term change levels. It does not provide details of individual '
        'term changes, which can be found in the GitHub milestone upon which the '
        'public review was based.'
    )
    report_lines.append('')
    report_lines.append('## Release')
    report_lines.append('')
    report_lines.append('- **Standard version:** `' + current_standard_version + '`')
    report_lines.append('- **Previous Standard version:** ' + (
        '`' + previous_standard_version + '`' if previous_standard_version else 'None'
    ))
    report_lines.append('- **Darwin Core Vocabulary version:** `' + current_vocabulary_version + '`')
    report_lines.append('- **Previous Vocabulary version:** ' + (
        '`' + previous_vocabulary_version + '`' if previous_vocabulary_version else 'None'
    ))
    report_lines.append('')

    report_lines.append('## Darwin Core Vocabulary composition')
    report_lines.append('')
    report_lines.append('| Term List | Version incorporated | Change from previous release |')
    report_lines.append('| --- | --- | --- |')
    for identity, display, change, previous, current in list_rows:
        if change == 'Removed':
            version_text = display['date'] + ' (not incorporated in this release)'
        else:
            version_text = display['date']
        report_lines.append(
            '| ' + display['label'].replace('|', '\\|') +
            ' (`' + identity + '`) | ' + version_text + ' | ' + change + ' |'
        )
    report_lines.append('')

    report_lines.append('## Changes from the previous Vocabulary version')
    report_lines.append('')
    for category in ('Added', 'Updated', 'Removed', 'Unchanged'):
        matching = [row for row in list_rows if row[2] == category]
        report_lines.append('### ' + category + ' Term Lists')
        report_lines.append('')
        if not matching:
            report_lines.append('None.')
        else:
            for identity, display, change, previous, current in matching:
                # Vocabulary membership changes and namespace term changes are
                # independent: newly incorporated lists can contain modified terms.
                results = [
                    result for result in namespace_results
                    if result['term_list_identity'] == identity
                ] if category != 'Removed' else []
                if len(results) > 1 or (category == 'Updated' and not results):
                    raise ValueError(
                        'Release report expected exactly one namespace result for '
                        'updated or configured Term List ' + identity +
                        '; found ' + str(len(results))
                    )
                if results:
                    if category == 'Updated':
                        version_text = previous['date'] + ' → ' + current['date']
                    else:
                        version_text = 'version ' + display['date']
                    report_lines.append(
                        '#### ' + display['label'] + ' (`' + identity + '`): ' +
                        version_text
                    )
                    report_lines.append('')
                    result = results[0]
                    for heading, key in (
                        ('Terms added', 'new_terms'),
                        ('Terms modified', 'modified_terms'),
                    ):
                        report_lines.append('##### ' + heading)
                        report_lines.append('')
                        terms = result[key]
                        if terms:
                            for term in terms:
                                report_lines.append('- `' + result['prefix'] + ':' + term + '`')
                        else:
                            report_lines.append('None.')
                        report_lines.append('')
                else:
                    report_lines.append(
                        '- **' + display['label'] + '** (`' + identity + '`), version ' +
                        display['date']
                    )
        report_lines.append('')

    report_lines.append('## Standard composition')
    report_lines.append('')
    report_lines.append('| Part version | Change from previous Standard version |')
    report_lines.append('| --- | --- |')
    for part_uri, change, previous_part in standard_rows:
        label = labels_by_version.get(part_uri, part_uri)
        if change == 'Updated' and previous_part:
            previous_label = labels_by_version.get(previous_part, previous_part)
            display = label + '<br>`' + part_uri + '`<br>replaces ' + previous_label + ' (`' + previous_part + '`)'
        elif change == 'Removed':
            display = label + '<br>`' + part_uri + '` (not incorporated in this release)'
        else:
            display = label + '<br>`' + part_uri + '`'
        report_lines.append('| ' + display.replace('|', '\\|') + ' | ' + change + ' |')
    report_lines.append('')

    report_lines.append('## Structural validation')
    report_lines.append('')
    report_lines.append('- Current Standard version exists uniquely: **PASS**')
    report_lines.append('- Current Vocabulary version exists uniquely: **PASS**')
    report_lines.append('- All current Vocabulary members resolve to Term List versions: **PASS**')
    report_lines.append('- Current Vocabulary contains at most one version of each Term List: **PASS**')
    report_lines.append('- Previous-version comparisons are based on explicit replacement metadata: **PASS**')
    report_lines.append('')
    report_lines.append(
        '_Generated by `process.py` from the resulting rs.tdwg.org metadata. '
        'Intended for review and for adaptation as the GitHub Release description after ratification._'
    )
    report_lines.append('')

    os.makedirs('reports', exist_ok=True)
    report_filename = os.path.join('reports', 'release-report-' + date_issued + '.md')
    with open(report_filename, 'w', encoding='utf-8') as report_file:
        report_file.write('\n'.join(report_lines))
    print('wrote release report', report_filename)
    return report_filename


release_report_filename = generate_release_report()

# -----------------------
# Write a concise audit log for the completed processing run
# -----------------------

repository_snapshot_after = snapshot_repository_files()
created_files = sorted(set(repository_snapshot_after) - set(repository_snapshot_before))
modified_files = sorted(
    path for path in set(repository_snapshot_after) & set(repository_snapshot_before)
    if repository_snapshot_after[path] != repository_snapshot_before[path]
)

run_completed = datetime.datetime.now()
repository_root = os.path.abspath('..')

def display_path(path):
    return os.path.relpath(path, os.getcwd())

os.makedirs('logs', exist_ok=True)
log_filename = os.path.join('logs', 'process-' + date_issued + '.log')

with open(log_filename, 'w', encoding='utf-8') as log_file:
    log_file.write('Darwin Core vocabulary processing\n')
    log_file.write('Release date: ' + date_issued + '\n')
    log_file.write('Started: ' + run_started.strftime('%Y-%m-%dT%H:%M:%S') + local_offset_from_utc + '\n')
    log_file.write('Completed: ' + run_completed.strftime('%Y-%m-%dT%H:%M:%S') + local_offset_from_utc + '\n')
    log_file.write('Status: SUCCESS\n\n')
    log_file.write('Revision directory: ' + release_directory + '/\n\n')

    log_file.write('NAMESPACE SUMMARY\n\n')
    for result in namespace_results:
        log_file.write(result['prefix'] + ' (' + result['namespace_uri'] + ')\n')
        log_file.write('  Input: ' + result['input_file'] + '\n')
        log_file.write('  New terms: ' + str(len(result['new_terms'])) + '\n')
        log_file.write('  Modified terms: ' + str(len(result['modified_terms'])) + '\n')
        if result['new_term_list']:
            log_file.write('  Action: processed as a new term list\n')
        elif result['changed']:
            log_file.write('  Action: processed term changes and version metadata\n')
        else:
            log_file.write('  Action: no term changes; existing current terms and versions carried forward\n')
        if result['new_terms']:
            log_file.write('  New term local names: ' + ', '.join(result['new_terms']) + '\n')
        if result['modified_terms']:
            log_file.write('  Modified term local names: ' + ', '.join(result['modified_terms']) + '\n')
        log_file.write('\n')

    log_file.write('FILES CREATED\n')
    if created_files:
        for path in created_files:
            log_file.write('  ' + display_path(path) + '\n')
    else:
        log_file.write('  None\n')

    log_file.write('\nFILES MODIFIED\n')
    if modified_files:
        for path in modified_files:
            log_file.write('  ' + display_path(path) + '\n')
    else:
        log_file.write('  None\n')

    namespaces_changed = sum(1 for result in namespace_results if result['changed'])
    namespaces_unchanged = len(namespace_results) - namespaces_changed
    total_new_terms = sum(len(result['new_terms']) for result in namespace_results)
    total_modified_terms = sum(len(result['modified_terms']) for result in namespace_results)

    log_file.write('\nSUMMARY\n')
    log_file.write('  Namespaces processed: ' + str(len(namespace_results)) + '\n')
    log_file.write('  Namespaces with changes/new term lists: ' + str(namespaces_changed) + '\n')
    log_file.write('  Namespaces unchanged: ' + str(namespaces_unchanged) + '\n')
    log_file.write('  New terms: ' + str(total_new_terms) + '\n')
    log_file.write('  Modified terms: ' + str(total_modified_terms) + '\n')
    log_file.write('  Files created: ' + str(len(created_files)) + '\n')
    log_file.write('  Files modified: ' + str(len(modified_files)) + '\n')

print('wrote processing log', log_filename)
