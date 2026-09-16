from discovery_parser import extract_transfer_rows,extract_tx_hashes

H="0x"+"a"*64
A="0x"+"1"*40
B="0x"+"2"*40
T="0x"+"3"*40

def test_transfer_parse():
    data={"items":[{"transaction_hash":H,"from":A,"to":B,"token":{"address":T},"block_number":123}]}
    rows=extract_transfer_rows(data)
    assert len(rows)==1
    assert rows[0]["to"]==B
    assert rows[0]["token"]==T

def test_hash_parse():
    data={"activity":[{"tx_hash":H}]}
    assert extract_tx_hashes(data)==[H]

if __name__=="__main__":
    test_transfer_parse();test_hash_parse();print("ok")
