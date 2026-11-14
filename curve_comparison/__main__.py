import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from .client import Client
from .repository import Repository
from .service import make_server


def main():
    parser = argparse.ArgumentParser(description='本地资料导入与业务概览')
    subcommands = parser.add_subparsers(dest='command', required=True)
    server_parser = subcommands.add_parser('serve', help='启动资料接口')
    server_parser.add_argument('--database', default='/tmp/analysis-data.sqlite3')
    server_parser.add_argument('--host', default='127.0.0.1')
    server_parser.add_argument('--port', type=int, default=8080)
    server_parser.add_argument('--load', type=Path)
    demo_parser = subcommands.add_parser('demo', help='运行本地资料往返演示')
    demo_parser.add_argument('--fixture', type=Path, default=Path('fixtures/demo.json'))
    args = parser.parse_args()
    if args.command == 'serve':
        repository = Repository(args.database)
        if args.load:
            repository.import_data(json.loads(args.load.read_text()), 'startup')
        server = make_server(repository, args.host, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / 'data.sqlite3')
            server = make_server(repository)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = Client(f'http://127.0.0.1:{server.server_port}')
                data = json.loads(args.fixture.read_text())
                receipt = client.import_data(data, 'demo')
                print(json.dumps({'import': receipt, 'summary': client.summary(receipt['revision'])}, ensure_ascii=False, indent=2))
            finally:
                server.shutdown()
                worker.join()
                server.server_close()


if __name__ == '__main__':
    main()
