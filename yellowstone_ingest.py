"""Alchemy Yellowstone gRPC Pump.fun transaction ingress. Read-only; no signing/submission."""
import os,threading,asyncio
def start_yellowstone(on_signature,pump_program,on_status):
    endpoint=os.getenv("YELLOWSTONE_ENDPOINT","").strip();token=os.getenv("YELLOWSTONE_X_TOKEN","").strip()
    if not endpoint or not token:return False
    threading.Thread(target=lambda:asyncio.run(_run(endpoint,token,on_signature,pump_program,on_status)),daemon=True,name="yellowstone-grpc").start();return True
async def _run(endpoint,token,on_signature,pump_program,on_status):
    import grpc
    from yellowstone_grpc import GeyserStub,SubscribeRequest,SubscribeRequestFilterTransactions,CommitmentLevel
    from solders.signature import Signature
    backoff=1
    while True:
        try:
            target=endpoint.replace("https://","").replace("http://","").rstrip("/")
            creds=grpc.ssl_channel_credentials();auth=grpc.metadata_call_credentials(lambda context,cb:cb((("x-token",token),),None))
            channel=grpc.aio.secure_channel(target,grpc.composite_channel_credentials(creds,auth));stub=GeyserStub(channel)
            req=SubscribeRequest(commitment=CommitmentLevel.PROCESSED);req.transactions["pump"].CopyFrom(SubscribeRequestFilterTransactions(vote=False,failed=False,account_include=[pump_program]))
            async def requests():
                yield req
                while True:await asyncio.sleep(30)
            on_status("CONNECTED PRIMARY");backoff=1
            async for msg in stub.Subscribe(requests()):
                tx=getattr(msg,"transaction",None);info=getattr(tx,"transaction",None) if tx else None;sig=getattr(info,"signature",b"") if info else b""
                if sig:
                    try:on_signature(str(Signature.from_bytes(bytes(sig))),"YELLOWSTONE_GRPC")
                    except Exception:pass
        except Exception as ex:on_status("RECONNECT "+type(ex).__name__+" "+str(ex)[:80]);await asyncio.sleep(backoff);backoff=min(30,backoff*2)
