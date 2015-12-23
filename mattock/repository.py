#!/usr/bin/python
#Copyright (c) 2015, Rob J Meijer.
#Copyright (c) 2015, University College Dublin
#All rights reserved.
#
#Redistribution and use in source and binary forms, with or without
#modification, are permitted provided that the following conditions are met:
#1. Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
#2. Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
#3. All advertising materials mentioning features or use of this software
#   must display the following acknowledgement:
#   This product includes software developed by the <organization>.
#4. Neither the name of the <organization> nor the
#   names of its contributors may be used to endorse or promote products
#   derived from this software without specific prior written permission.
#
#THIS SOFTWARE IS PROVIDED BY <COPYRIGHT HOLDER> ''AS IS'' AND ANY
#EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
#DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE
#
import errno
import copy
import os
import fcntl
import carvpath
import refcount_stack
import opportunistic_hash

try:
  from os import posix_fadvise,POSIX_FADV_DONTNEED,POSIX_FADV_NORMAL
except:
  try:  
    from fadvise import posix_fadvise,POSIX_FADV_DONTNEED,POSIX_FADV_NORMAL
  except:
    import sys
    print("")
    print("\033[93mERROR:\033[0m fadvise module not installed. Please install fadvise python module. Run:")
    print("")
    print("    sudo pip install fadvise")
    print("")
    sys.exit()

#Functor class for fadvise on an archive fd.
class _FadviseFunctor:
  def __init__(self,fd):
    self.fd=fd
  def __call__(self,offset,size,willneed):
    if willneed:
      posix_fadvise(self.fd,offset,size,POSIX_FADV_NORMAL)
    else:
      posix_fadvise(self.fd,offset,size,POSIX_FADV_DONTNEED)

#RAII class for keeping a file lock during sparse grow operations.
class _RaiiFLock:
  def __init__(self,fd):
    self.fd=fd
    fcntl.flock(self.fd,fcntl.LOCK_EX)
  def __del__(self):
    fcntl.flock(self.fd,fcntl.LOCK_UN)

#Class representing an open file. This can either be a mutable or a carvpath file.
class _OpenFile:
  def __init__(self,stack,cp,entity,fd):
    self.cp=cp
    self.stack=stack
    self.ohashcollection=self.stack.ohashcollection
    self.stack.add_carvpath(cp)
    self.entity=entity
    self.fd=fd
    self.refcount=1
  def __del__(self):
    self.stack.remove_carvpath(self.cp)
  def atomic_read(self,chunk):
    #In python 3 we could do this:
    #return os.pread(self.fd,chunk.size,chunk.offset)
    #FIXME, we need a lock here if we want to support multi-threader operations
    os.lseek(self.fd, chunk.offset, 0)
    return os.read(self.fd, chunk.size)
  def atomic_write(self,chunk,chunkdata):
    #FIXME, we need a lock here if we want to support multi-threader operations
    os.lseek(self.fd, chunk.offset, 0)
    os.write(self.fd,chunkdata)
    return
  def read(self,offset,size):
    readent=self.entity.subentity(carvpath._Entity(self.entity.longpathmap,self.entity.maxfstoken,[carvpath.Fragment(offset,size)]),True)
    result=b''
    for chunk in readent:
      datachunk = self.atomic_read(chunk)
      result += datachunk
      self.ohashcollection.lowlevel_read_data(chunk.offset,datachunk)
    return result
  def write(self,offset,data):
    size=len(data)
    writeent=self.entity.subentity(carvpath._Entity(self.entity.longpathmap,self.entity.maxfstoken,[carvpath.Fragment(offset,size)]),True)
    dataindex=0
    for chunk in writeent:
      chunkdata=data[dataindex:dataindex+chunk.size]
      dataindex += chunk.size
      self.atomic_write(chunk,chunkdata)
      self.ohashcollection.lowlevel_written_data(chunk.offset,chunkdata)
    return size

class Repository:
  def __init__(self,reppath,context,ohash_log,refcount_log):
    self.context=context
    #Create a new opportunistic hash collection.
    self.col=opportunistic_hash.OpportunisticHashCollection(context,ohash_log)
    #We start off with zero open files
    self.openfiles={}
    #Open the underlying data file and create if needed.
    self.fd=os.open(reppath,(os.O_RDWR | os.O_LARGEFILE | os.O_NOATIME | os.O_CREAT))
    #Get the current repository total size.
    cursize=os.lseek(self.fd,0,os.SEEK_END) 
    #Set the entire repository as dontneed and assume everything to be cold data for now.
    posix_fadvise(self.fd,0,cursize,POSIX_FADV_DONTNEED)
    #Create CarvPath top entity of the proper size.
    self.top=self.context.make_top(cursize)
    #Create fadvise functor from fd.
    fadvise=_FadviseFunctor(self.fd)
    #Create a referencecounting carvpath stack using our fadvise functor and ohash collection.
    self.stack=refcount_stack.CarvpathRefcountStack(self.context,fadvise,self.col,refcount_log)
  def __del__(self):
    #On destruction close the underlying file.
    os.close(self.fd)
  def _grow(self,chunksize):
    #Use a file lock to atomically allocate a new chunk of (at first sparse) file data.
    l=_RaiiFLock(self.fd)
    cursize=os.lseek(self.fd,0,os.SEEK_END)
    os.ftruncate(self.fd,cursize+chunksize)
    self.top.grow(cursize + chunksize - self.top.size)
    return cursize
  #It multiple instances of MattockFS use the smae repository, sync archive size with the underlying file size.
  def multi_sync(self):
    cursize=os.lseek(self.fd,0,os.SEEK_END)
    grown = cursize - self.top.size
    self.top.grow(grown)
    return grown
  #Allocate a new piece of mutable data and return CarvPath
  def newmutable(self,chunksize):
    chunkoffset=self._grow(chunksize) 
    cp=str(carvpath._Entity(self.context.longpathmap,self.context.maxfstoken,[carvpath.Fragment(chunkoffset,chunksize)])) 
    return cp
  def volume(self):
    if len(self.stack.fragmentrefstack) == 0:
      return 0
    return self.stack.fragmentrefstack[0].totalsize
  def full_archive_size(self):
    return self.top.size
  def validcarvpath(self,cp):
    try:
      ent=self.context.parse(cp)
      rval=self.top.test(ent)
      return rval
    except:
      return False
  def flatten(self,basecp,subcp):
    try:
      ent=self.context.parse(basecp + "/" + subcp)
      return str(ent)
    except:
      return None
  def anycast_set_volume(self,anycast):
    volume=0
    for jobid in anycast:
      cp = anycast[jobid].carvpath
      volume += self.context.parse(anycast[jobid].carvpath).totalsize
    return volume
  def anycast_best(self,actorname,anycast,sort_policy):
    if len(anycast) > 0:
      cp2key={}
      for anycastkey in anycast.keys():
        cp=anycast[anycastkey].carvpath
        cp2key[cp]=anycastkey
      bestcp=self.stack.priority_custompick(sort_policy,intransit=cp2key.keys()) 
      return cp2key[bestcp]
    return None
  def getTopThrottleInfo(self):
    totalsize=self.top.size
    normal=self.volume()
    dontneed=totalsize-normal
    return (normal,dontneed)
  def anycast_best_actors(self,actorsstate,actorset,letter):
    fetchvolume=False
    if letter in "VDC":
      fetchvolume=True
    bestactors=set()
    bestval=0
    for actor in actorset.actors.keys():
      anycast = actorset.actors[actor].anycast
      overflow = actorset.actors[actor].overflow
      if len(anycast) > overflow:
        volume=0
        if fetchvolume:
          volume=self.anycast_set_volume(anycast)
        val=0
        if letter == "S":
          val=len(anycast)
        if letter == "V":
          val=volume
        if letter == "D":
          if volume > 0:
            val=float(len(anycast))/float(volume)
          else:
            if len(anycast) > 0:
               return 100*len(anycast)
        if letter == "W":
          val=actorsstate[actor].weight
        if letter == "C":
          if volume > 0:
            val=float(actorsstate[actor].weight)*float(len(anycast))/float(volume)
          else:
            if len(anycast) > 0:
              return 100*len(anycast)*actorsstate[actor].weight
        if val > bestval:
          bestval=val
          bestactors=set()
        if val == bestval:
          bestactors.add(actors)
    return bestactors
  def open(self,carvpath,path):
    ent=self.context.parse(carvpath)
    if path in self.openfiles:
      self.openfiles[path].refcount += 1
    else :
      ent=self.context.parse(carvpath)
      self.openfiles[path]=_OpenFile(self.stack,carvpath,ent,self.fd)
    return 0
  def read(self,path,offset,size):
    if path in self.openfiles: 
      return self.openfiles[path].read(offset,size)
    return -errno.EIO
  def write(self,path,offset,data):
    if path in self.openfiles:
      return self.openfiles[path].write(offset,data)
    return -errno.EIO
  def flush(self):
    return os.fsync(self.fd)
  def close(self,path):
    self.openfiles[path].refcount -= 1
    if self.openfiles[path].refcount < 1:
      del self.openfiles[path]
    return 0
    

if __name__ == "__main__":
  import carvpath
  import opportunistic_hash
  context=carvpath.Context({},160)
  rep=Repository("/var/mattock/archive/0.dd",context,"test3.log","test4.log")
  entity=context.parse("1234+5678")
  f1=rep.open("1234+5678","/frozen/1234+5678.dat")
  print rep.volume() 
