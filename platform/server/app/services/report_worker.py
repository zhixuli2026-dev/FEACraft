"""Retired unregistered worker; formal builds use registered_report_worker."""


def main():
    raise SystemExit('Legacy report worker is retired; submit a registered report build through the API')


if __name__ == "__main__":
    main()
