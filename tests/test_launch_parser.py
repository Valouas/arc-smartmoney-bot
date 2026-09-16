from launch_parser import extract_contract_candidates, extract_tokens_from_transfers

def test_contract_parser():
    data = {"items":[{"address":"0x"+"1"*40,"symbol":"ABC","standard":"ERC-20","block_number":123}]}
    rows = extract_contract_candidates(data)
    assert len(rows)==1
    assert rows[0]["symbol"]=="ABC"
    assert rows[0]["deployment_block"]==123

def test_transfer_parser():
    data = {"data":[{"token":{"address":"0x"+"2"*40,"symbol":"XYZ","name":"Xyz"}}]}
    rows = extract_tokens_from_transfers(data)
    assert len(rows)==1
    assert rows[0]["symbol"]=="XYZ"

if __name__ == "__main__":
    test_contract_parser(); test_transfer_parser(); print("ok")
