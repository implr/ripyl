#!/usr/bin/python
# -*- coding: utf-8 -*-

'''Ripyl protocol decode library
   I2C protocol decoder
'''

# Copyright © 2013 Kevin Thibedeau

# This file is part of Ripyl.

# Ripyl is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.

# Ripyl is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with Ripyl. If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, division

import itertools

from ripyl.decode import *
from ripyl.streaming import *
from ripyl.util.enum import Enum
from ripyl.util.bitops import *
from ripyl.sigproc import remove_excess_edges

class I2C(Enum):
    '''Enumeration for I2C r/w bit states'''
    Write = 0
    Read = 1


class I2CByte(StreamSegment):
    '''Segment for a byte of I2C data'''
    def __init__(self, bounds, data=None, ack_bit=None):
        StreamSegment.__init__(self, bounds, data)
        self.kind = 'I2C byte'
        self.ack_bit = ack_bit
        
    def __str__(self):
        return str(self.data)

        
class I2CAddress(StreamSegment):
    '''Segment for an I2C address
    
    The byte(s) conposing the address are contained as subrecords
    '''
    def __init__(self, bounds, address=None, r_wn=None):
        '''
        r_wn
            Read (1) / Write (0) bit
        '''
        StreamSegment.__init__(self, bounds, address)
        self.kind = 'I2C address'
        self.r_wn = r_wn
        
    @property
    def address(self):
        return self.data
        
    @address.setter
    def address(self, value):
        self.data = value
        
    def __str__(self):
        return str(hex(self.data))


def i2c_decode(scl, sda, stream_type=StreamType.Samples):
    '''Decode an I2C data stream
    
    This is a generator function that can be used in a pipeline of waveform
    processing operations.
    
    scl
        An iterable representing the I2C serial clock
    
    sda
        An iterable representing the I2C serial data
    
    stream_type
        Indicates the type of stream used for scl and sda.
        
        When StreamType.Samples, the iterators represent a sequence of samples.
        Each sample is a 2-tuple representing the time of the sample and the sample's
        value. When this type is used, the scl stream is analyzed to determine the
        logic levels of the two streams.
        
        When StreamType.Edges, the iterators represent a series of edges.
        scl and sda are iterables of 2-tuples representing each edge transition.
        The 2-tuples *must* be in the absolute time form (time, logic level).

    
    Yields a series of StreamRecord-based objects. These will be one of three event types
      or two data types. The three events are represented by StreamEvent object with these
      obj.kind attribute values:
        * 'I2C start'   The start of an I2C transfer
        * 'I2C restart' A start condition during a transfer
        * 'I2C stop'    The end of a transfer
        
      The two data types are represented by the objects I2CAddress and I2CByte. The former
      is a 7-bit or 10-bit address from the start of a transfer or restart. The latter contains
      the data read or written during the transfer. I2CByte has an attribute ack_bit that
      records the value of the ACK for that byte. I2CAddress has a r_wn attribute that indicates
      if the transfer is a read or write. The subrecords attribute contains the I2CByte object
      or objects that composed the address.
    
    Raises AutoLevelError when the stream_type is Samples and the logic levels cannot
      be determined automatically.
    '''
    
    if stream_type == StreamType.Samples:
        # tee off an iterator to determine logic thresholds
        s_scl_it, thresh_it = itertools.tee(scl)
        
        logic = find_logic_levels(thresh_it)
        if logic is None:
            raise AutoLevelError
        del thresh_it
        
        hyst = 0.4
        scl_it = find_edges(s_scl_it, logic, hysteresis=hyst)
        sda_it = find_edges(sda, logic, hysteresis=hyst)

    else: # the streams are already lists of edges
        scl_it = scl
        sda_it = sda


    edge_sets = {
        'scl': scl_it,
        'sda': sda_it
    }
    
    es = MultiEdgeSequence(edge_sets, 0.0)
    
    S_IDLE = 0
    S_ADDR = 1
    S_ADDR_10B = 2
    S_DATA = 3

    state = S_IDLE
    
    start_time = None
    end_time = None
    bits = []
    prev_10b_addr = None
    
    while not es.at_end():
    
        ts, cname = es.advance_to_edge()
        
        if state == S_IDLE:
            bits = []
            if cname == 'sda' and not es.at_end('sda') \
            and es.cur_state('sda') == 0 and es.cur_state('scl') == 1:
                # start condition met
                se = StreamEvent(es.cur_time(), data=None, kind='I2C start')
                yield se
                state = S_ADDR
                
        else:
            # check for stop and restart
            if cname == 'sda' and not es.at_end('sda'):
                if es.cur_state('sda') == 1 and es.cur_state('scl') == 1:
                    # stop condition met
                    se = StreamEvent(es.cur_time(), data=None, kind='I2C stop')
                    yield se
                    state = S_IDLE
                    continue
                    
                if es.cur_state('sda') == 0 and es.cur_state('scl') == 1:
                    # restart condition met
                    se = StreamEvent(es.cur_time(), data=None, kind='I2C restart')
                    yield se
                    bits = []
                    start_time = None
                    state = S_ADDR
                    continue

            if cname == 'scl' and not es.at_end('scl') and es.cur_state('scl') == 1:
                # rising edge of SCL
                
                # accumulate the bit
                if start_time is None:
                    start_time = es.cur_time()
    
                bits.append(es.cur_state('sda'))
                end_time = es.cur_time()
                
                if len(bits) == 9:
                    word = join_bits(bits[0:8])
                    ack_bit = bits[8]

                    if state == S_ADDR:
                        addr = word >> 1
                        r_wn = word & 0x01
                        if addr > 0x77: # first 2 bits of 10-bit address
                            if r_wn: # 10-bit addressed read
                                # We will not receive the second byte of the address
                                # The 10-bit address being read should be the last one
                                # written to.
                                if prev_10b_addr is not None:
                                    addr_10b = prev_10b_addr.data
                                    
                                    # Check that the upper bits match
                                    ub = addr & 0x03
                                    prev_ub = (addr_10b >> 8) & 0x03
                                    if ub != prev_ub: # This shouldn't happen
                                        addr_10b = 0xFFF # invalid address
                                    
                                else: # This shouldn't happen
                                    addr_10b = 0xFFF # invalid address
                                
                                nb = I2CByte((start_time, end_time), word, ack_bit)
                                na = I2CAddress((start_time, end_time), addr_10b, r_wn)
                                na.subrecords.append(nb)
                                yield na
                                bits = []
                                start_time = None
                                
                                state = S_DATA
                            
                            else: # 10-bit addressed write
                                first_addr = I2CByte((start_time, end_time), word, ack_bit)
                                bits = []
                                state = S_ADDR_10B
                            
                        else: # 7-bit address
                            r_wn = word & 0x01
                            nb = I2CByte((start_time, end_time), word, ack_bit)
                            na = I2CAddress((start_time, end_time), addr, r_wn)
                            na.subrecords.append(nb)
                            yield na
                            bits = []
                            start_time = None

                            state = S_DATA

                    elif state == S_ADDR_10B: # 10-bit address
                        addr = (((first_addr.data >> 1) & 0x03) << 8) | word
                        r_wn = first_addr.data & 0x01
                        ab2 = I2CByte((start_time, end_time), word, ack_bit)
                        na = I2CAddress((first_addr.start_time, end_time), addr, r_wn)
                        na.subrecords.append(first_addr)
                        na.subrecords.append(ab2)
                        
                        prev_10b_addr = na
                        yield na
                        bits = []
                        start_time = None

                        state = S_DATA
                                    

                    else: # S_DATA
                        nb = I2CByte((start_time, end_time), word, ack_bit)
                        yield nb
                        bits = []
                        start_time = None               


class I2CTransfer(object):
    '''Represent a transaction over the I2C bus'''
    def __init__(self, r_wn, address, data):
        '''
        r_wn
            Read/write mode for the transfer
        
        address
            Address of the transfer. Can be either a 7-bit or 10-bit address.
        
        data
            Array of bytes sent in the transfer
        '''
        self.r_wn = r_wn
        self.address = address
        self.data = data
        
    def bytes(self):
        '''Get a list of raw bytes including the formatted address'''
        b = []
        
        if self.address <= 0x77: # 7-bit address
            b.append((self.address << 1) | (self.r_wn & 0x01))
        
        else: # 10-bit address
            address_upper_bits = (self.address & 0x300) >> 8
            address_lower_bits = self.address & 0xFF
            
            b.append((0x78 | address_upper_bits) << 1 | (self.r_wn & 0x01))
            if not self.r_wn: # write: send both bytes
                b.append(address_lower_bits)

        b.extend(self.data)
        return b
        
    def ack_bits(self):
        '''Generate a list of ack bits for each byte of data'''
        ack = []
        
        if self.address <= 0x77:
            ack.append(0)
        else:
            ack.extend([0, 0])

        ack.extend([0] * len(self.data))
        if self.r_wn == I2C.Read:
            ack[-1] = 1 # Master nacks last byte of a read
            
        return ack
        
    def __repr__(self):
        return 'I2CTransfer({0}, {1}, {2})'.format(self.r_wn, hex(self.address), self.data)
        
    def __eq__(self, other):
        match = True
        
        if self.r_wn != other.r_wn:
            match = False
            
        if self.address != other.address:
            match = False
            
        if self.data != other.data:
            match = False
            
        return match
        
    def __ne__(self, other):
        return not self == other

        
def reconstruct_i2c_transfers(records):
    '''Recreate a I2CTransfer objects from the output of i2c_decode()

    This is a generator function that can be used in a pipeline of waveform
    processing operations.

    records
        An iterable of I2CByte and I2CAddress records as produced by i2c_decode()
        
    Yields a stream of I2CTransfer objects containing aggregated address and data
      from the input records.
    '''
    S_ADDR = 0
    S_DATA = 1
    
    state = S_ADDR
    cur_addr = None
    cur_data = []

    for r in records:
        
        if state == S_ADDR:
            if r.kind == 'I2C address':
                cur_addr = r
                state = S_DATA
               
        elif state == S_DATA:
            if r.kind == 'I2C byte':
                cur_data.append(r)
            
            if r.kind == 'I2C address':
                # reconstruct previous transfer
                tfer = I2CTransfer(cur_addr.r_wn, cur_addr.address, [b.data for b in cur_data])
                yield tfer
                
                cur_addr = r
                cur_data = []
            
    if cur_addr is not None:
        # reconstruct last transfer
        tfer = I2CTransfer(cur_addr.r_wn, cur_addr.address, [b.data for b in cur_data])
        yield tfer


def i2c_synth(transfers, clock_freq, idle_start=0.0, idle_end=0.0):
    '''Generate synthesized I2C waveforms
    
    This function simulates I2C transfers on the SCL and SDA signals.

    transfers
        An iterable of I2CTransfer objects containing data to be synthesized.
    
    clock_freq
        Floating point clock frequency for the I2C bus. Standard rates are 100kHz (100.0e3)
        and 400kHz (400.0e3) but any frequency can be specified.
    
    idle_start
        The amount of idle time before the transmission of transfers begins
    
    idle_end
        The amount of idle time after the last transfer
    
    Returns a pair of iterators representing the two edge streams for scl, and sda
      respectively. Each edge stream pair is in (time, value) format representing the
      time and logic value (0 or 1) for each edge transition. The first elements in the
      iterators are the initial state of the waveforms.
    '''
    # This is a wrapper around the actual synthesis code in _i2c_synth()
    # It unzips the yielded tuple and removes unwanted artifact edges
    scl, sda = itertools.izip(*_i2c_synth(transfers, clock_freq, idle_start, idle_end))
    scl = remove_excess_edges(scl)
    sda = remove_excess_edges(sda)
    return scl, sda


def _i2c_synth(transfers, clock_freq, idle_start=0.0, idle_end=0.0):
    '''Core I2C synthesizer
    
    This is a generator function.
    '''

    t = 0.0
    sda = 1
    scl = 1
    
    half_bit_period = 1.0 / (2.0 * clock_freq)
    
    yield ((t, scl), (t, sda)) # initial conditions
    t += idle_start
    
    for i, tfer in enumerate(transfers):
        # generate start
        sda = 0
        yield ((t, scl), (t, sda))
        
        t += half_bit_period / 2.0
        scl = 0
        yield ((t, scl), (t, sda))
        
        t += half_bit_period / 2.0
    
        ack_bits = tfer.ack_bits()
        for j, byte in enumerate(tfer.bytes()):
            bits = split_bits(byte, 8)
            bits.append(ack_bits[j])
            
            for b in bits:
                sda = b
                yield ((t, scl), (t, sda))
                t += half_bit_period / 2.0
                
                scl = 1
                yield ((t, scl), (t, sda))
                t += half_bit_period
                
                scl = 0
                yield ((t, scl), (t, sda))
                t += half_bit_period / 2.0
                
        # Prep for repeated start unless last transfer
        if i < len(transfers)-1:
            sda = 1
            yield ((t, scl), (t, sda))
            t += half_bit_period / 2.0
            
            scl = 1
            yield ((t, scl), (t, sda))
            t += half_bit_period / 2.0
            
    # generate stop after last transfer
    sda = 0
    yield ((t, scl), (t, sda))
    t += half_bit_period / 2.0
    
    scl = 1
    yield ((t, scl), (t, sda))
    t += half_bit_period / 2.0
    
    sda = 1
    yield ((t, scl), (t, sda))
    t += half_bit_period / 2.0 + idle_end
    
    yield ((t, scl), (t, sda)) # final state
            
            
                
