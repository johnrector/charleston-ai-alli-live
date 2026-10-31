"""Small finite speech conversion; preparation only, never the live audio path."""
from array import array
import math
import sys


def ulaw_encode(value):
    sign=0x80 if value<0 else 0
    value=min(abs(int(value))+132,32635)
    exponent=7
    mask=0x4000
    while exponent and not value&mask:
        exponent-=1;mask>>=1
    return (~(sign | exponent<<4 | (value>>(exponent+3)&15)))&255


def ulaw_decode(value):
    value=(~value)&255
    sample=(((value&15)<<3)+132) << ((value>>4)&7)
    return 132-sample if value&128 else sample-132


DECODE=tuple(ulaw_decode(n) for n in range(256))


def pcm24_to_ulaw8(data):
    if not isinstance(data,bytes) or not data or len(data)%2 or len(data)>24_000*2*60:
        raise ValueError('Finite mono PCM24 must be positive, complete and at most sixty seconds')
    data+=b'\0'*((6-len(data)%6)%6)
    samples=array('h');samples.frombytes(data)
    if sys.byteorder!='little':samples.byteswap()
    # Windowed low-pass before decimation; keeps aliases out of telephone audio.
    size=45;middle=size//2;cutoff=3400/24000
    coefficients=[]
    for n in range(size):
        x=n-middle
        sinc=2*cutoff if not x else math.sin(2*math.pi*cutoff*x)/(math.pi*x)
        coefficients.append(sinc*(.54-.46*math.cos(2*math.pi*n/(size-1))))
    norm=sum(coefficients);coefficients=[x/norm for x in coefficients]
    padded=[0]*middle+samples.tolist()+[0]*middle
    return bytes(ulaw_encode(sum(padded[i+j]*coefficients[j] for j in range(size)))
                 for i in range(0,len(samples),3))


def nonquiet_audio(encoded):
    """Conservative interrupt signal, not a human/voicemail classifier."""
    if not encoded:return False
    return sum(DECODE[n]**2 for n in encoded)>len(encoded)*100**2
