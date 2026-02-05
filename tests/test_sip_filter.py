import sys
import os
import unittest
import logging

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Mock aiortc/aiohttp if needed, but assuming they are present in venv
from speechcloud.sip_aiortc import AiortcSIPHandler

class TestSIPFilter(unittest.TestCase):
    def test_filter_private(self):
        handler = AiortcSIPHandler()
        
        # Case 1: Host private, srflx present -> Filter host
        sdp_in = """v=0
a=candidate:1 1 udp 2130706431 192.168.1.201 58948 typ host
a=candidate:2 1 udp 1694498815 109.164.39.209 58948 typ srflx raddr 192.168.1.201 rport 58948
a=end-of-candidates
"""
        
        expected = """v=0
a=candidate:2 1 udp 1694498815 109.164.39.209 58948 typ srflx raddr 192.168.1.201 rport 58948
a=end-of-candidates
"""
        
        result = handler._filter_private_candidates(sdp_in)
        # Normalize line endings
        self.assertEqual(result.replace('\r\n', '\n').strip(), expected.strip())
        
    def test_no_srflx(self):
        handler = AiortcSIPHandler()
        
        # Case 2: Only host private -> Do NOT filter
        sdp_in = """v=0
a=candidate:1 1 udp 2130706431 192.168.1.201 58948 typ host
"""
        
        result = handler._filter_private_candidates(sdp_in)
        self.assertEqual(result.replace('\r\n', '\n').strip(), sdp_in.strip())

    def test_public_host(self):
        handler = AiortcSIPHandler()
        
        # Case 3: Host is public (e.g. VPS) -> Do NOT filter
        sdp_in = """v=0
a=candidate:1 1 udp 2130706431 8.8.8.8 58948 typ host
a=candidate:2 1 udp 1694498815 109.164.39.209 58948 typ srflx raddr 8.8.8.8 rport 58948
"""
        
        result = handler._filter_private_candidates(sdp_in)
        self.assertEqual(result.replace('\r\n', '\n').strip(), sdp_in.strip())
        
    def test_complex_sdp(self):
        handler = AiortcSIPHandler()
        sdp_in = """v=0
o=- 3979204588 3979204588 IN IP4 0.0.0.0
s=-
t=0 0
a=group:BUNDLE 0
a=msid-semantic:WMS *
m=audio 58948 UDP/TLS/RTP/SAVPF 0
c=IN IP4 192.168.1.201
a=sendrecv
a=rtcp:9 IN IP4 0.0.0.0
a=candidate:04196f03415d24ccf1aff816c64d4a38 1 udp 2130706431 192.168.1.201 58948 typ host
a=candidate:8ef8b0668270812f1c6621b8305c42b1 1 udp 1694498815 109.164.39.209 58948 typ srflx raddr 192.168.1.201 rport 58948
a=end-of-candidates
a=ice-ufrag:uE8P
a=ice-pwd:cLe4w1R6ZD2iItDAqeYaxm
"""
        result = handler._filter_private_candidates(sdp_in)
        self.assertNotIn("192.168.1.201 58948 typ host", result)
        self.assertIn("typ srflx", result)
        # Verify c= line updated
        self.assertIn("c=IN IP4 109.164.39.209", result)
        self.assertNotIn("c=IN IP4 192.168.1.201", result)
        # Verify bundle/mux removed
        self.assertNotIn("a=group:BUNDLE", result)
        self.assertNotIn("a=rtcp-mux", result)
        # Verify structure
        self.assertIn("a=candidate:8ef8b0668270812f1c6621b8305c42b1 1 udp", result)

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
