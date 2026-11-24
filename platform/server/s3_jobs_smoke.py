"""J15: isolated S3 Jobs, private execution and versioned typed publication.

Public typed commands now use publication contracts, so the old staging-only
assertions no longer describe their behavior. Legacy staging merge semantics
remain covered by s3_staging_smoke; this entry runs the actual HTTP paths.
"""
import os


def main():
    if os.environ.get('VIBEFLOW_TEST_S3')!='1':
        print('J15 S3 suites require VIBEFLOW_TEST_S3=1 [SKIP]')
        return
    from s3_attempt_smoke import main as ordinary
    from s3_typed_publication_smoke import main as typed
    ordinary()
    typed()
    print('J15 isolated real S3 Jobs and publication suites passed')


if __name__=='__main__':main()
