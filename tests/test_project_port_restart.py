import os
import socket
import unittest
from human_compact.trajectory import project_ports as P

class PortRestartTests(unittest.TestCase):
    def plan(self,port):
        return {'services':[{'id':'web','healthUrl':f'http://127.0.0.1:{port}/'}]}

    def test_live_listener_is_still_a_conflict(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            server.bind(('127.0.0.1',0));server.listen()
            self.assertTrue(P.conflicts(self.plan(server.getsockname()[1])))

    @unittest.skipIf(os.name=='nt','POSIX TIME_WAIT restart semantics')
    def test_closed_http_connection_does_not_block_repair(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            server.bind(('127.0.0.1',0));server.listen();port=server.getsockname()[1]
            with socket.create_connection(('127.0.0.1',port)) as client:
                accepted,_=server.accept()
                with accepted:accepted.sendall(b'x')
                self.assertEqual(client.recv(1),b'x')
                self.assertEqual(client.recv(1),b'')
        self.assertEqual(P.conflicts(self.plan(port)),[])
        with socket.socket() as replacement:
            replacement.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            replacement.bind(('127.0.0.1',port));replacement.listen()

class PreparationReuseTests(unittest.TestCase):
    def test_only_successful_unchanged_port_failure_preparation(self):
        step={'stage':'prepare','cwd':'/tmp','argv':['npm','run','build'],'env':{}}
        stages=[{'status':'done','cwd':'/tmp','command':'npm run build'}]
        failure={'stderr':'Error: listen EADDRINUSE :::3200'}
        self.assertTrue(P.reusable_preparation(step,stages,failure,'/tmp'))
        for changed in ({**step,'env':{'PORT':'3201'}},{**step,'cwd':'/'},{**step,'stage':'service'}):
            self.assertFalse(P.reusable_preparation(changed,stages,failure,'/tmp'))
        self.assertFalse(P.reusable_preparation(step,stages,{'stderr':'compile failed'},'/tmp'))
        self.assertFalse(P.reusable_preparation(step,[],failure,'/tmp'))
