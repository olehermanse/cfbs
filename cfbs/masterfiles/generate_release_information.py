# TODO document `cfbs generate-release-information`
# it generates the .json data files in the cwd
import sys

from cfbs.masterfiles.download_all_versions import download_all_versions_enterprise
from cfbs.masterfiles.check_tarball_checksums import check_tarball_checksums
from cfbs.masterfiles.generate_vcf_download import generate_vcf_download
from cfbs.masterfiles.generate_vcf_git_checkout import generate_vcf_git_checkout

# commented out for now as this adds an extra dependency in its current state (dictdiffer)
# from cfbs.masterfiles.check_download_matches_git import check_download_matches_git


def generate_release_information():
    print("Downloading Enterprise masterfiles...")
    output_path, downloaded_versions, reported_checksums = (
        download_all_versions_enterprise()
    )
    # TODO Community coverage:
    # downloaded_versions, reported_checksums = download_all_versions_community()

    # Enterprise 3.9.2 is downloaded but there is no reported checksum, so both args are necessary
    if check_tarball_checksums(output_path, downloaded_versions, reported_checksums):
        print("Every checksum matches")
    else:
        print("Checksums differ!")
        sys.exit(1)

    generate_vcf_download(output_path, downloaded_versions)
    generate_vcf_git_checkout(downloaded_versions)

    # TODO automatic analysis of the difference between downloadable MPF data and git MPF data
    # in its current state, this generates differences-*.txt files for each version
    # check_download_matches_git(downloaded_versions)
