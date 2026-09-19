"""Alchemy Yellowstone gRPC Pump.fun ingress. Protobuf-native hot path; no RPC getTransaction."""
import os,threading,asyncio,pathlib,time
def _proto():
    try:import geyser_pb2,geyser_pb2_grpc;return geyser_pb2,geyser_pb2_grpc
    except ImportError:
        from grpc_tools import protoc
        import grpc_tools
        root=pathlib.Path(__file__).resolve().parent;inc=pathlib.Path(grpc_tools.__file__).resolve().parent/"_proto"
        rc=protoc.main(["protoc",f"-I{root/'proto'}",f"-I{inc}",f"--python_out={root}",f"--grpc_python_out={root}",str(root/"proto/geyser.proto"),str(root/"proto/solana-storage.proto")])
        if rc:raise RuntimeError("PROTOC_FAILED_"+str(rc))
        import geyser_pb2,geyser_pb2_grpc;return geyser_pb2,geyser_pb2_grpc
def start_yellowstone(on_event,pump_program,on_status):
    endpoint=os.getenv("YELLOWSTONE_ENDPOINT","").strip();token=os.getenv("YELLOWSTONE_X_TOKEN","").strip()
    if not endpoint or not token:return False
    threading.Thread(target=lambda:asyncio.run(_run(endpoint,token,on_event,pump_program,on_status)),daemon=True,name="yellowstone-grpc").start();return True
async def _run(endpoint,token,on_event,pump_program,on_status):
    import grpc
    from solders.signature import Signature
    pb,stubmod=_proto();backoff=1
    while True:
        try:
            target=endpoint.replace("https://","").replace("http://","").rstrip("/")
            auth=grpc.metadata_call_credentials(lambda _,cb:cb((("x-token",token),),None))
            channel=grpc.aio.secure_channel(target,grpc.composite_channel_credentials(grpc.ssl_channel_credentials(),auth),
                options=[("grpc.keepalive_time_ms",30000),("grpc.keepalive_timeout_ms",10000),("grpc.keepalive_permit_without_calls",1)])
            stub=stubmod.GeyserStub(channel);req=pb.SubscribeRequest(commitment=pb.PROCESSED)
            req.transactions["pump"].CopyFrom(pb.SubscribeRequestFilterTransactions(vote=False,failed=False,account_include=[pump_program]))
            req.slots["slots"].CopyFrom(pb.SubscribeRequestFilterSlots(filter_by_commitment=True))
            async def requests():
                yield req
                while True:
                    await asyncio.sleep(20);yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1))
            on_status("CONNECTED PRIMARY");backoff=1
            async for msg in stub.Subscribe(requests()):
                if msg.HasField("slot"):
                    on_status("CONNECTED PRIMARY slot="+str(msg.slot.slot));continue
                if not msg.HasField("transaction"):continue
                u=msg.transaction;info=u.transaction;sig=bytes(info.signature)
                if not sig:continue
                meta=info.meta
                logs=list(meta.log_messages)
                pre=[{"mint":b.mint} for b in meta.pre_token_balances]
                post=[{"mint":b.mint} for b in meta.post_token_balances]
                keys=[]
                try:keys=[str(__import__("solders").pubkey.Pubkey.from_bytes(bytes(k))) for k in info.transaction.message.account_keys]
                except Exception:pass
                on_event({"sig":str(Signature.from_bytes(sig)),"slot":int(u.slot),"logs":logs,"pre":pre,"post":post,"account_keys":keys,"received_ns":time.time_ns(),"source":"YELLOWSTONE_GRPC"})
        except Exception as ex:
            on_status("RECONNECT "+type(ex).__name__+" "+str(ex)[:100]);await asyncio.sleep(backoff);backoff=min(30,backoff*2)
