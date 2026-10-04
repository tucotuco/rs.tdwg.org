# Processing vocabulary changes

**Title:** Processing vocabulary changes

**Date Modified:** 2026-10-01

**Part of TDWG Standard:** Not part of any standard

**Abstract:** Once vocabulary developers have defined terms using a spreadsheet, the data in that spreadsheet can be processed into other forms used to generate human and machine readable representations of the data in the spreadsheet. This document provides information about how to use scripts to generate those representations.

**Contributors:** Steve Baskauf (TDWG Technical Architecture Group, TDWG Audiovisual Core Maintenance Group, TDWG Darwin Core Maintenance Group), John Weiczorek (TDWG Darwin Core Maintenance Group)

# Table of Contents

[1 Introduction](#1-introduction)

[2 Using this document](#2-using-this-document)

[3 Detailed workflow steps](#3-detailed-workflow-steps)

[4 Build script for a human readable List of Terms document](#4-build-script-for-a-human-readable-list-of-terms-document)

[5 Generating JSON-LD for controlled vocabularies](#5-generating-json-ld-for-controlled-vocabularies)

[6 Reference](#6-reference)

[7 Metadata membership and version snapshots](#7-metadata-membership-and-version-snapshots)

# 1 Introduction

## 1.1 RFC 2119 statement

The key words “MUST”, “MUST NOT”, “REQUIRED”, “SHALL”, “SHALL NOT”, “SHOULD”, “SHOULD NOT”, “RECOMMENDED”, “MAY”, and “OPTIONAL” in this document are to be interpreted as described in [BCP 14](https://www.rfc-editor.org/info/bcp14) [[RFC 2119]](https://datatracker.ietf.org/doc/html/rfc2119) and [[RFC 8174]](https://datatracker.ietf.org/doc/html/rfc8174) when, and only when, they appear in all capitals, as shown here.

Use of 2119 keywords is not an indication that compliance is required by any TDWG standard. Rather, it is an indication that the associated software will not function as designed if the user does not comply with the requirements of this document.

## 1.2 Audience

This document is intended for those who are responsible for maintaining the TDWG infrastructure. It can also be used by anyone who is developing a vocabulary and wants to generate draft term list documents from a hand-generated CSV file.

## 1.3 Background

The [TDWG Standards Documentation Specification](http://rs.tdwg.org/sds/doc/specification/) (SDS) indicates that all human and machine readable representations of vocabulary components should provide the same data. That can be achieved by using a script to generate those representations from common data sources: CSV files generated from a [basic hand-generated CSV file created by the vocabulary developers](create-vocabulary.md) and YAML files containing metadata about the components. The process is similar regardless of whether it is a new vocabulary or if modifications are being made to an existing vocabulary. 

The majority of this document is focused on the process of generating the underlying data used to construct List of Terms documents. However, standards can also include documents other than Lists of Terms e.g., Darwin Core includes Text, XML, and RDF guides. Although these documents do not contain lists of terms, they do have basic document-level metadata that needs to be managed and made available when the document IRIs are dereferenced via a request for machine-readable RDF metadata. For List of Terms documents associated with vocabulary processing, `process.py` now performs the document metadata update as part of the same processing run. The document metadata processor can still be run independently for documents that are not Lists of Terms.

NOTE: term deprecations cannot be carried out using this workflow and they require a number of special steps. See the [notes at the start of the detailed Jupyter notebook](process_rs_tdwg_org.ipynb) for specific steps that are necessary for term deprecations. In general, term deprecations should be avoided unless absolutely necessary.

![overview of workflow](images/overview_workflow_diagram.jpg)

The diagram above shows a high-level view of how the hand-edited CSV file is combined with author and document metadata (in the form of YAML files provided by the vocabulary maintainers) to generate machine-readable metadata (in the form of RDF) and a human-readable List of Terms document. Ideally, this process involves a back-and-forth between the vocabulary maintainers (e.g. a Maintenance Group) and those who maintain the TDWG infrastructure (in particular, the rs.tdwg.org GitHub repository). The maintainers provide the raw data in the form of the hand-edited CSV and YAML files, which are then processed by script to generate the necessary authoritative files in the rs.tdwg.org repo. These authoritative files can then be used by the maintainers to generate (by script) an updated List of Terms document on their ancillary website. 

The same workflow can be used to generate draft documents using a fork of the rs.tdwg.org repo prior to final ratification of term changes. In that case the maintainers can simulate the entire process without merging the changes into the `master` branch of rs.tdwg.org . See [Section 2.3 below](https://github.com/tdwg/rs.tdwg.org/blob/master/process/process-vocabulary.md#23-generating-drafts) for details.

The following diagrams show some of the details of the steps shown in the overview above.

![generation of metadata tables](images/table-generation.png)

A Python script ("A" in the overview diagram) uses the data present in the hand-generated CSV files to generate several CSV files ("2" in the overview diagram) that contain all of the metadata required by the SDS. The data are used to generate specific term versions and to update the current terms by automatically adding some fields that are generated by the script. The script also links the versions to the current terms in a join table.

![generation of machine readable metadata](images/machine-readable-mapping.png)

Data in the generated current terms CSV file is used with a mapping table to generate machine-readable metadata ("6" in the overview diagram) about the terms. The mapping table is hand-edited as necessary when the vocabulary is first created and relates the header names in the current terms CSV file to the abbreviated property IRIs used in the machine readable representation.

![generation of human readable document](images/human-readable-mapping.png)

### 1.3.1 Human readable document listing terms

The current terms CSV file ("2" in the overview diagram) and metadata YAML files ("3" in the overview diagram) can also be used along with a Python build script ("E" in the diagram above) to create a human readable document listing terms and their metadata (a "List of Terms" document; "8" in the diagram above). The List of Terms build script is managed by the Maintenance Group, so the details of its operation vary by vocabulary. However, to ensure that the principle that metadata in any serialization is the same, the script MUST draw from the authoritative CSV files in rs.tdwg.org (or the appropriate current [document_configuration.yaml document configuration files](document_metadata_processing)) to ensure that is the case. 

**Technical note:** There is a distinction between this document listing terms and a "term list" document. *Term list* is a technical term defined in [section 3.3.3 of the SDS](http://rs.tdwg.org/sds/doc/specification/) denoting a list of terms incorporated into a vocabulary that share a common namespace. Therefore, a "term list" document is a document that describes all of the terms included in a term list. The document listing terms that is described here may or may not be the same as a "term list" document since it can include terms from a single namespace or terms from an entire vocabulary that consists of multiple term lists. Documents listing terms in a vocabulary are typically called "List of Terms" documents.

During the initial vocabulary development process, a List of Terms build script can be used to generate drafts for review. Re-running the build script will cause changes or corrections made to the hand generated CSV file to be reflected in a revised document listing terms. Typically, the drafts are managed in a fork of rs.tdwg.org, since the developing task group will generally not have write access to rs.tdwg.org . See [Section 2.3 below](https://github.com/tdwg/rs.tdwg.org/blob/master/process/process-vocabulary.md#23-generating-drafts) for details.

### 1.3.2 Redirection for human-readable term metadata during content negotiation

For term IRIs, redirection during content-negotiation for machine-readable representations is handled automatically by the server, since those representations are generated directly from the metadata stored in the rs.tdwg.org repo. However, the human-readable representations redirect to fragment identifiers in List of Terms documents. For currently maintained vocabularies, these are usually GitHub Pages-generated web pages whose actual page URLs do not correspond to the term IRIs. Correct redirection is controlled by the redirect URL in the [`redirects.csv` file](https://github.com/tdwg/rs.tdwg.org/blob/master/html/redirects.csv) in the `html` directory of the `rs.tdwg.org`. The `process.py` processing script will update this table automatically using data from the appropriate `config.yaml`, file but vocabulary maintainers MUST make sure that the fragment identifiers in the List of Terms documents they generate follow a pattern that can be specified in the `config.yaml` file. 

The convention as of 2026-03-17 is to construct the fragment identifier by concatenating the namespace abbreviation + "_" + the local name. For example, the fragment identifier for `dwc:recordedBy` would be `dwc_recordedBy` (case of both namespace and local name preserved). The redirect URL would then be the actual page URL + "#" + the fragment identifier. In this example, the redirect URL for <http://rs.tdwg.org/dwc/terms/recordedBy> would be <https://dwc.tdwg.org/list/#dwc_recordedBy>. An example of the appropriate field values to specify this construction are [here](https://github.com/tdwg/rs.tdwg.org/blob/ab0fb23b55aa47b141d84e2c0f9b8ea882692e12/process/dwc-revisions/dwc-revisions-2025-07-10/config.yaml#L89-L100). The necessary tagging in the raw Markdown file to set the fragment identifier in the list of terms document is shown [here](https://github.com/tdwg/dwc/blob/237bf09e293cef0b001734d259ddcd3d43ac96b0/docs/list/index.md?plain=1#L14352).

# 2 Using this document

Since this document is intended for use by those who are responsible for maintaining the TDWG infrastructure, it is assumed that the user has a basic understanding of the TDWG infrastructure and the standards that are maintained by TDWG. 

## 2.1 Skills required

To carry out the process described in this document, you need to know how:
- to use Git and GitHub. The simplest way to carry out the necessary operations is to download the [GitHub Desktop client](https://desktop.github.com/). An introduction to Git and GitHub is [here](http://vanderbi.lt/github).
- to edit a YAML configuration file using a text editor.
- to run a Python script, and have Python installed on your local computer.

![workflow diagram](workflow.jpg)

## 2.2 Required inputs

After cloning the rs.tdwg.org repository (or a fork of it) to your local drive, the principal processing script is `process.py`. For vocabulary/List of Terms processing, this script processes the hand-generated vocabulary metadata CSV files and automatically invokes the document metadata update as part of the same run. The reusable `tdwg_docs_metadata_update.py` module remains available in the `document_metadata_processing` directory and can also be run independently when only human-readable document metadata needs to be updated.

Prior to beginning the processing steps, several data files are required. These include the underlying metadata and some configuration files:

- an `authors_configuration.yaml` file that contains metadata about the authors of the document. 
- a `document_configuration.yaml` file that contains metadata about the document.
- a separate hand-generated CSV file for each namespace to be processed (vocabularies/List of Terms only). Each hand generated file represents a term list. The term lists MUST be part of the same vocabulary. Processing of multiple vocabularies requires separate processing runs.
- a `config.yaml` file that contains the configuration settings for the processing script (vocabularies/List of Terms only). This file is used to specify the location of the hand-generated CSV files and to specify how the processing script should process the data in those files. The configuration file also contains some term list-level metadata. 
- a `vocab.yaml` file that contains metadata about the vocabulary and standard that include the term changes (vocabularies/List of Terms only).

An additional file, `general_configuration.yaml`, is required for updating the metadata about human-readable documents. It is generated/updated automatically as part of `process.py` when processing the List of Terms document associated with a vocabulary. It must be edited manually when `tdwg_docs_metadata_update.py` is run independently for another (non-List of Terms) document.

## 2.3 Generating drafts

The workflow below describes how changes to terms in a vocabulary would be made by maintainers of the rs.tdwg.org repository, starting with the hand-edited source CSV files containing the changes, and ending with a new release of the rs.tdwg.org repo, resulting in the changes going "live" (i.e. term IRIs dereferencing to updated human-readable Lists of Terms or machine-readable RDF). This assumes that the changes have been finalized and ratified through the change process described in the [TDWG Vocabulary Maintenance Specification](http://rs.tdwg.org/vms/doc/specification/).

The same workflow can also be used before ratification to generate draft metadata and List of Terms documents for proofreading, soliciting comments, and presentation to the Executive Committee. A member of a Task Group or Maintenance Group who does not have write access to the TDWG rs.tdwg.org repository can carry out the workflow in a personal fork and use the processed branch as the data source for the relevant List of Terms build script.

Earlier versions of the processing workflow required special branch management when revising drafts. Because processing modified repository metadata incrementally, a maintainer generally had to return to the unprocessed source state, correct the source files, create a new derived branch, and process again. This is no longer necessary.

The current `process.py` is designed to be repeatable on the same working branch. Processing is performed in a staged temporary workspace and the generated metadata changes are applied to the working repository only after processing succeeds. Before publication, the script verifies that affected working-tree paths have not changed since processing began. Publication is transactional: if publication fails after one or more files have been replaced, the affected paths are restored to their pre-run state. Processing logs and release reports are published only after the metadata transaction succeeds. If processing fails, generated changes from that run are therefore not left partially applied to the repository.

If the same inputs are processed again, no additional metadata changes are generated. If valid but incorrect source data or configuration produced an unwanted result, the maintainer can correct those inputs and run `process.py` again; the resulting metadata converges on the state represented by the corrected inputs. At the Vocabulary and Standard levels, this is achieved by reconstructing complete version snapshots from authoritative current membership rather than by copying a previous version and replacing only the member that changed.

The normal draft-development cycle is therefore to create a working branch, edit the source CSV/YAML files, run `process.py`, inspect the resulting diffs, and repeat the edit/run/inspect cycle on that same branch until the results are satisfactory. It is still good Git practice to make appropriate commits and to keep source inputs clearly identifiable, but deleting and recreating a derived branch between processing attempts is no longer part of the required workflow.

Once the draft is final, the source inputs can be submitted and reviewed according to the normal TDWG process. After ratification, rs.tdwg.org maintainers should verify that `date_issued` in `config.yaml` contains the ratification date, run `process.py`, inspect the generated metadata, and commit the resulting source and derived changes. The Maintenance Group can then use the authoritative metadata to build and publish the final List of Terms document.

# 3 Detailed workflow steps

**Important note:** a vocabulary-processing run has one configured parent Vocabulary and one parent Standard. It can process multiple Term Lists/namespaces that belong to that Vocabulary, including Term Lists whose namespace IRIs differ from the Vocabulary IRI. Processing changes belonging to two different parent Vocabularies still requires separate processing runs. For example, changes to terms in the Variant Controlled Vocabulary and in one or more Term Lists in the main Audiovisual Core Vocabulary require separate vocabulary-processing runs, even though both Vocabularies are parts of the Audiovisual Core Standard. For a Vocabulary update, `process.py` also updates the metadata for the associated List of Terms document automatically. If metadata for another document in the same Standard must also be updated, run the standalone `tdwg_docs_metadata_update.py` workflow separately after the vocabulary processing so that the applicable Standard version already exists. In the edge case where the only update to a Standard is a non-List of Terms document, or where the Standard does not include a List of Terms document, the existing document-only workflow may still require manual preparation of the applicable Standard/version metadata.

1. If you are not a maintainer of rs.tdwg.org, first fork the [rs.tdwg.org](https://github.com/tdwg/rs.tdwg.org) repository to your account so that you have write access for the changes you make. Clone the forked repository to your local drive.
2. Create a new working branch of the repository. A name pattern like `ac-changes-2026-02-15` can help you keep track of the branch. The processing scripts operate entirely on the local repository; pushing intermediate commits to GitHub is not required. If you are creating or updating a human-readable document that is not a List of Terms document, prepare the document configuration described in steps 7 and 8 and use the standalone document metadata processor rather than the vocabulary workflow.
3. There are [generic example spreadsheets](https://github.com/tdwg/rs.tdwg.org/tree/master/process/example-spreadsheets) of hand-edited CSV spreadsheets that can be used as examples and to obtain the appropriate column headers. For more information, see the [instructions for creating a vocabulary](https://github.com/tdwg/rs.tdwg.org/blob/master/process/create-vocabulary.md#user-content-3-details-and-examples). Place the hand-generated namespace CSV files in the appropriate release-specific revision directory under `process`, following the pattern used by existing standards. Unless you are creating a new vocabulary, it is best to start with a hand-generated CSV from a previous revision and delete the data rows so that the correct column headers are retained. When updating existing terms, copy relevant cells from the existing primary metadata CSV file to minimize typographical changes to fields that are not intended to change.
4. Prepare `config.yaml`. For a new vocabulary, use the [`config.yaml` file](https://github.com/tdwg/rs.tdwg.org/blob/master/process/config.yaml) in the `process` directory as a template. For an existing vocabulary, a recent configuration stored with an earlier revision is generally a better starting point, provided that it contains the current configuration fields.
5. Enter the general configuration settings and the settings for each namespace participating in the vocabulary processing run. Follow the detailed comments in the YAML file. Each configured namespace must have the expected release input CSV, even if that CSV contains only its header and there are no changes for that namespace. Save the configuration with the release source files for future reference.
6. Prepare [`vocab.yaml`](https://github.com/tdwg/rs.tdwg.org/blob/master/process/vocab.yaml), which contains metadata about the vocabulary and standard. For an existing vocabulary it will generally not need to change. Changes to existing vocabulary or standard metadata in this file are reflected in the generated metadata, so reuse the most recent applicable configuration unless a metadata change is intentional.
7. Three YAML configuration files provide information about the human-readable document, often a List of Terms. `general_configuration.yaml` is in the `document_metadata_processing` directory. For the List of Terms document associated with a vocabulary processing run, `process.py` supplies the applicable release information and invokes the document metadata update automatically; the maintainer does not run the document processor separately. For other documents, `general_configuration.yaml` must be prepared for the standalone document-processing workflow.
8. The other two document configuration files are stored in a subdirectory of `document_metadata_processing` corresponding to the document's permanent IRI. For example, `http://rs.tdwg.org/dwc/doc/list/` corresponds to `dwc_doc_list`. The `authors_configuration.yaml` file supplies author metadata and `document_configuration.yaml` supplies document metadata. Existing files need to be edited only when their source metadata changes; version-specific mutable metadata is generated during processing.
9. If you are creating a new vocabulary and the hand-edited CSV contains columns for additional properties beyond those required by the Standards Documentation Specification, manually edit the column header mapping file as described in section 3.1.
10. At this point all source data required for processing should be in place. Making a commit here is RECOMMENDED because it provides a useful Git checkpoint, but the processing architecture no longer requires returning to this commit between iterations.
11. Run [`process.py`](https://github.com/tdwg/rs.tdwg.org/blob/master/process/process.py) from the `process` directory. The script processes vocabulary metadata, updates term IRI redirect metadata in [`redirects.csv`](https://github.com/tdwg/rs.tdwg.org/blob/master/html/redirects.csv), and updates the metadata for the associated List of Terms document in the same operation.
12. Before modifying metadata, `process.py` performs preflight validation of the relevant current and historical metadata. Among other checks, it detects ambiguous duplicate current identities, duplicate dated versions, and invalid or ambiguous version histories that would prevent deterministic reconstruction of the release. A preflight failure terminates processing without publishing generated metadata.
13. `process.py` performs the release processing in a staged temporary workspace. After staged processing succeeds, it verifies that affected working-tree paths have not changed concurrently and then publishes the metadata as a transaction. If publication itself fails, affected paths are rolled back to their pre-run state. The processing log and generated release report are published only after successful metadata publication.
14. After a successful run, carefully examine the diffs for all changed files and inspect the generated release report in `process/reports/`. If something is wrong with otherwise valid source data, configuration, or persistent membership metadata, correct those inputs on the same working branch and run `process.py` again. Processing is designed to converge on the state represented by the current inputs: an unchanged rerun produces no additional metadata changes, and a corrected rerun replaces the previously generated result with the result corresponding to the corrected inputs. Deleting and recreating the branch is not required.
15. When the generated metadata is satisfactory, commit the source and derived changes. If the goal is to generate a draft List of Terms document, push the branch to the fork as necessary and use that branch as the source for the Maintenance Group's List of Terms build process. The edit/run/inspect cycle can be repeated on the same branch as the draft evolves.
16. If you are not a maintainer of rs.tdwg.org and the changes are being submitted to the Executive Committee for ratification, create the appropriate pull request containing the source inputs according to the Maintenance Group's release procedure. After ratification, rs.tdwg.org maintainers should ensure that the configured `date_issued` is the ratification date, run `process.py`, inspect the results, and commit the processed metadata.
17. After the processed changes are merged to the master branch, term dereferencing for machine-readable metadata can be tested using the rs-test.tdwg.org server. For example, if `http://rs.tdwg.org/eco/terms/protocolNames` was added or modified, `http://rs-test.tdwg.org/eco/terms/protocolNames.rdf` should return RDF/XML containing the changes. There can be a delay between merging changes and their appearance on the test server.
18. After testing, inform the Maintenance Group that the final metadata are available so that the authoritative List of Terms can be published on the standard's website. A new release of the rs.tdwg.org repository triggers deployment to the production server. Ideally, publication of the List of Terms precedes or coincides with that release so that redirects for new terms resolve to valid fragment identifiers. Server deployment and front-end caching can introduce additional delay before changes are visible.


## 3.1 Modifying the column header mapping file

Because the SDS requires particular properties to be included in term metadata, if the template hand-generated CSV file is used without editing the column headers, a template column header mapping file can be used as well. The column header mapping file only needs to be modified if additional property columns are added to the template CSV file. This may happen if specialty properties are added to the required properties.

Controlled vocabularies contain one or more additional properties that are not found in vocabularies that define properties and classes. That includes the controlled value string and may also include a property to indicate that a value has a `broader` relationship to another concept. So controlled vocabularies should use one of the template column header mapping files designed for controlled vocabularies. Setting the value of `vocab_type` in the configuration section determines whether the mapping template includes mappings for these extra term columns or not. See section 2.1.1 for details.

If additional property columns were added to the hand-generated CSV file, the mapping file in the current terms directory for that term list (i.e. the directory created having the name set as the value of `database` in the configuration section) must be manually edited. The name of the mapping file ends in `-mappings.csv`. 

The order of rows in the mapping file does not matter. The first column (`header`) contains the name of the column header in the hand-generated CSV file. The second column (`predicate`) contains the abbreviated IRI (also known as [CURIE](https://www.w3.org/TR/curie/) or [QName](https://www.w3.org/2001/tag/doc/qnameids)). If the namespace abbreviation of an added row is different from others already present in this column, check the `namespace.csv` file in the same directory to make sure that the abbreviation is already listed. If not, add it to that list of namespace abbreviations and IRIs. The third column, which describes the type of the value in the column, MUST have one of the following strings as its value: `iri`, `language`, `datatype`, or `plain`. For language-tagged strings, the `attribute` column contains the ISO 639-1 language code used in the tag. For strings having a `datatype`, the `attribute` column contains the abbreviated IRI for the datatype. If the column in the CSV file contains an unabbreviated full IRI, there is no value in the `value` column of the mapping table. If the column in the CSV contains the local name part of the IRI, the `value` column contains full namespace IRI to be prepended to the value from column in the CSV. 

It is also possible to generate a fixed value for all rows in the CSV table. See [this page](https://github.com/baskaufs/guid-o-matic/blob/master/use.md#recording-the-column-mappings-from--the-metadata-table-to-rdf-triples) for more details on the format of the mapping file. 

## 3.2 Legacy notebooks and term deprecations

There are two Python scripts in Jupyter notebooks that were used to develop the script and formerly used to do the processing. They are no longer maintained, but contain a lot of comments that might help in understanding what the script does. They may also be useable for term deprecations. They are:

1. The [simplified processing script](simplified_process_rs_tdwg_org.ipynb) presupposes no knowledge of Python and will work for most term additions and changes in existing standards and for creating simple vocabularies or term lists, including controlled vocabularies. **You MUST NOT use this script for term deprecations.**
2. Because this script is not designed for use by the general public, it has limited error trapping. In cases where results are not as expected, or where unusual changes such as term deprecations are required, the [full processing script](process_rs_tdwg_org.ipynb) SHOULD be used. This script contains the same code as the simplified script, but separates the code among more cells and provides more feedback in the form of print statements. **Note on 2024-03-01: Since this script was written, the processing script has been significantly modified. You should not assume that the full processing script notebook is usable without modification.**

We really should not be deprecating terms anyway, so there should be only rare cases where using the full processing script is necessary.

# 4 Build script for a human readable List of Terms document

**NOTE:** Since this section was originally written, the build scripts used by Audiovisual Core and Darwin Core (based originally on the scripts linked here) have been greatly modified to make it possible for language variants of the List of Terms documents to be generated in concert with the CrowdIn translation system. The material in this section has been left here for historical reasons, but any Maintenance Group creating a new build script of their List of Terms should base it off of one of the existing build scripts currently in use by one of the Maintenance Groups. As of 2026-03-17, the Audiovisual and Darwin Core scripts are nearly identical and support the CrowdIn system. The TCS build script has different source code, and supports building term metadata (not yet document metadata) from rs.tdwg.org, but at this point does not support CrowdIn. The Latimer Core build script has completely different source code and I think generates HTML directly (not Markdown), so what is described in the workflow regarding rendering of draft Markdown does not apply. I'm not sure exactly how the data are sourced and CrowdIn isn't currently supported. 

A document listing terms and their metadata (a "List of Terms" document) is a Markdown document consisting of two or more parts. The first part is a hand-edited template file that contains the introductory material (header section, introduction, RFC 2119 keywords section, etc.). The second part is created by a script that generates the actual list of terms from the current terms files for term lists that are included in the listing. The script is relatively simple if all terms are found in a single term list. It is more complex if the vocabulary includes terms from several term lists or if the terms are categorized. There are two example build scripts that can be modified by a Python programmer if modifications are needed to make the term list document conform to the idiosyncrasies of a given vocabulary.

## 4.1 Building a simple term list

The notebook `build-page-simple.ipynb` in the `process/page_build_scripts` directory of the rs.tdwg.org repository has an example set up for a controlled vocabulary with hierarchy. That directory also has a template Markdown file for the introductory section that can be modified as necessary.

## 4.2 Categorizing terms

It is reasonable to include the few terms of a simple vocabulary in a single section. However, documents listing the terms of larger and more complicated vocabularies may need to be organized into categories to make it easier to locate related terms. This approach was first used with Darwin Core and has also been adopted by Audubon Core. 

The key to organizing the terms in this way is by using the property `tdwgutility:organizedInClass` where the value is a class under which the subject is organized. NOTE: the local name of this property should not mislead users to think that grouping property terms in this way indicates that the grouped properties have been declared to have the organizing class as a domain. TDWG-minted terms SHOULD NOT have ranges or domains as part of their basic metadata.

In many cases, the organizing class will be a well-known class previously defined by TDWG or some other organization. Examples in Darwin Core are `dwc:Occurrence` and `dcterms:Location`. However, it is also possible to create a "convenience" class within the `tdwgutility:` namespace solely for the purpose of organizing related terms. For example, Audubon Core uses the class `tdwgutility:ResourceCreation` to group property terms related to the creation of multimedia resources. Terms in the `tdwgutility:` namespace are not generally governed by any standard, so organizational class terms can be added as necessary without going through any official change process.

### 4.2.1 Using categories

In order to use categories, edit the configuration section of the build script so that the value of `organized_in_categories` is `True`. Then create Python lists containing corresponding values for `display_order`, `display_labels`, `display_commnets`, and `display_id`. When the script builds the page, it will use these data to organize the terms and create appropriate section headings and notes for the categories. See the notebook `build-page-categories.ipynb` in the `process/page_build_scripts` directory of the rs.tdwg.org repository for an example.

# 5 Generating JSON-LD for controlled vocabularies

**NOTE:** As of 2026-03-17, making translations of Darwin Core and Audiovisual Core controlled vocabularies is being handled by the CrowdIn system, which commits translated CSV metadata files to rs.tdwg.org . The Audiovisual Core subjectPart and subjectOrientation controlled vocabularies are available in translation as JSON-LD as described below. However, it is unlikely that this will be maintained in the future. 

In order to make controlled vocabularies as widely available as possible, multi-lingual translations of the term labels and definitions should be made available in as many languages as possible. A Python script (build-json-ld.ipynb) to generate JSON-LD is available in the `cv_json_ld` directory. It can be run from any location, so maintenance groups should use it to generate JSON-LD representations of their controlled vocabularies on their own sites. This JSON-LD can then be used by developers to create multilingual tools to make it easier for users to select the right concept and acquire the controlled value string or IRI associated with that concept.

Because the JSON-LD can easily be ingested, it can also be used to build multilingual web applications. Some Javascript code and an HTML file for a simple web page is also available in the directory. To see the page in action, visit [this page](https://heardlibrary.github.io/digital-scholarship/lod/json_ld_test/display-cv.html). NOTE on 2024-03-04, the management of translations documentation is still being worked out.

# 6 Reference

## 6.1 Standards hierarchy

The TDWG standards hierarchy organizes resources at four major levels: standards, vocabularies, term lists, and terms. The hierarchy is shown in the diagram below. 

![TDWG metadata model](https://raw.githubusercontent.com/tdwg/vocab/master/tdwg-standards-hierarchy-2017-01-23.png)

Ratification of a term addition or change triggers new versions at all of the higher levels in the standards hierarchy. New term versions trigger new term list versions. New term list versions trigger new vocabulary versions and new vocabulary versions trigger new standards versions. For more information about versioning of TDWG standards, see [Section 2.3 of the TDWG Standards Documentation Specification](http://rs.tdwg.org/sds/doc/specification/).

## 6.2 Term versions

Each current term is related to at least one term version. If a current term is new, its record is created and the last-modified date is set to be the same as the created date. A dated version is also created for the current term, with an issued date that is the same as the last-modified date of the current term.

![TDWG versions model](https://github.com/tdwg/vocab/raw/master/graphics/version-model.png)

Each time a term's metadata is revised, a new version is created. The term version IRI is formed by appending the date of issue to the term local name. A `hasVersion` relationship is created between the term and its version, and the new version has a `replaces` relationship with the previous version. The metadata defining these relationships are generated by the processing script. Other properties such as the definition, usage, and notes are copied from the hand-generated CSV file edited by the creators/maintainers.

## 6.3 Assignment of term versions to a new term list version

A term list is a group of related terms that share the same namespace part of their IRI. As with all TDWG resources, term lists also have versions. When a term is changed or added, the new term version is added to a new version of the term list (replacing any older version if necessary). If a term is new, it is also added to the existing term list. 

## 6.4 Proliferation of new versions up the hierarchy

A term change can require new versions at each applicable level of the hierarchy: Term, Term List, Vocabulary, and Standard. However, the processing script does not construct a new Vocabulary or Standard version merely by copying the previous version and substituting the changed child resource. Instead, it reconstructs the complete target-date snapshot from the persistent current-membership tables described in section 7.

For each declared member of a Vocabulary, the processor resolves the Term List version that applies on the release date: a version issued on the release date is used when one exists; otherwise the latest prior version is carried forward. The same principle is used for the parts of a Standard. Thus unchanged Term Lists, Vocabularies, and Documents can be incorporated by reference to versions issued before the current release date. A membership change by itself can also require a new parent version even when no term metadata changed.

This distinction is important because the dated membership tables are historical snapshots, not the source of truth for current containment. Current containment is declared separately, and each new Vocabulary or Standard version is expected to be a complete snapshot of the membership that applies to that release.

# 7 Metadata membership and version snapshots

The rs.tdwg.org metadata distinguishes between **current membership declarations** and **membership of dated versions**. This distinction is important for understanding both the data model and the behavior of `process.py`.

Current membership tables state which resources are presently members or parts of higher-level resources. Dated membership tables record the complete membership of a particular version at a particular point in time. The current tables are therefore persistent configuration/state used to construct new snapshots; the dated tables are historical records and should not be treated as templates whose omissions are automatically inherited by later releases.

## 7.1 Term membership and versions

Current terms and their dated versions are stored in the database directory configured for each Term List. The processing script maintains the current term records, dated term-version records, and the join metadata relating current terms to their versions. A new or changed term can cause a new Term List version to be generated.

The membership of a dated Term List version records the applicable version of every term in that Term List. Unchanged terms are represented by carrying forward their latest applicable earlier term versions rather than by creating unnecessary new term versions.

## 7.2 Vocabulary membership

Current Term List membership in a Vocabulary is stored in:

`vocabularies/vocabularies-members.csv`

Each row declares that a current Term List is a member of a current Vocabulary. This table is the authoritative source used by `process.py` when reconstructing the membership of a new Vocabulary version.

Vocabulary versions are recorded in:

`vocabularies-versions/vocabularies-versions.csv`

The complete Term List membership of each dated Vocabulary version is stored in:

`vocabularies-versions/vocabularies-versions-members.csv`

When a target Vocabulary version is generated, `process.py` resolves every Term List declared in `vocabularies-members.csv` to the applicable Term List version on the release date. If a Term List has a version issued on the target date, that version is used; otherwise its latest unambiguous version issued before the target date is carried forward. The target Vocabulary membership rows are then written as a complete snapshot and checked against the declared current membership.

This means that a Term List can become a member of a Vocabulary even if none of its terms changes in that release. Conversely, removing a Term List from `vocabularies-members.csv` means that it is not included in newly reconstructed Vocabulary versions, while historical Vocabulary versions continue to retain their recorded historical membership.

## 7.3 Standard membership

Current parts of a Standard are stored in:

`standards/standards-parts.csv`

This table declares the resources that are presently parts of each Standard. Parts can include Vocabularies and Documents.

Standard versions are recorded in:

`standards-versions/standards-versions.csv`

The complete parts of each dated Standard version are stored in:

`standards-versions/standards-versions-parts.csv`

When a target Standard version is generated, `process.py` resolves each resource declared in `standards-parts.csv` to the applicable version on the release date. For a Vocabulary, the applicable version is resolved from the Vocabulary-version metadata. For a Document, the applicable version is resolved from the document-version metadata. A target-date version is used when one exists; otherwise the latest unambiguous prior version is carried forward. The resulting rows constitute the complete snapshot of the Standard at that version.

Removing a current Vocabulary or Document from `standards-parts.csv` therefore prevents it from being included in newly reconstructed Standard versions, but does not alter historical Standard snapshots in `standards-versions-parts.csv`.

## 7.4 Why current membership and historical snapshots are separate

Separating current membership from historical version membership serves two different purposes:

- the current membership tables express what belongs to a Vocabulary or Standard now and provide the authoritative containment input for the next release;
- the dated membership tables preserve what belonged to each particular historical version.

The processor validates histories sufficiently to resolve each declared member to one applicable version. Ambiguous duplicate identities or duplicate versions for the same resource and date are treated as errors rather than resolved by row order or other incidental properties of the CSV files.

This reconstruction model also prevents an omission in an older dated snapshot from automatically propagating into a new release. A new Vocabulary or Standard version is derived from the authoritative current membership declarations and the applicable dated versions of those members, not from the membership rows of its predecessor.

## 7.5 Processing log and release report

A successful vocabulary-processing run writes an operational log under `process/logs/` and a Markdown release report under `process/reports/`. These files are intentionally outside the metadata transaction itself. They are generated in the staged workspace and are published to the working repository only after the metadata transaction succeeds.

The release report summarizes the resulting Standard, Vocabulary, and Term List composition and compares it with the immediately preceding versions. It is intended to support review of a proposed release and can be adapted for use as a GitHub Release description after ratification. It does not replace inspection of the generated metadata diffs.

