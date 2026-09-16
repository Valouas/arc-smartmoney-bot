from decoder import classify_trade
from web3 import Web3

class FakeRPC:
    def token_meta(self, token): return {"symbol":"XYZ","decimals":18}

WALLET = Web3.to_checksum_address("0x0000000000000000000000000000000000000001")
TOKEN = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
ROUTER = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")

def topic(addr): return bytes.fromhex("00"*12 + addr[2:].lower())

def test_native_buy():
    receipt = {"logs":[{"address": TOKEN,"topics":[bytes(Web3.keccak(text="Transfer(address,address,uint256)")),topic(ROUTER), topic(WALLET)],"data": (10**18).to_bytes(32,"big")} ]}
    out = classify_trade(FakeRPC(), receipt, WALLET, 25, native_value_wei=100*10**18)
    assert len(out) == 1
    assert out[0]["side"] == "BUY"
    assert abs(out[0]["usdc_amount"] - 100) < 1e-9
    assert out[0]["confirmation_eligible"] is False  # Gross msg.value lacks refund evidence.

if __name__ == "__main__":
    test_native_buy(); print("ok")
