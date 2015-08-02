import ctypes
import xml.etree.ElementTree as ET
import requests
import sys
from geopy.distance import great_circle
import os

from twitterbot import TwitterBot,TwitterBotError


class ZipCodeError(Exception):
   '''Base class for zip code errors'''

   @property
   def message(self):
      '''Returns the first argument used to construct this error.'''
      return self.args[0]

def twos_comp_signed(val, bits=32):
   '''
   Compute the two's complement of a signed integer
   '''
   if val<0:
      # convert to unsigned
      val=ctypes.c_uint32(val).value
   # check if sign bit is set
   if (val & (1 << (bits - 1))) != 0:
      # compute negative value
      val-=(1 << bits)
   return val

def chunk_reverse_pad(value):
   # create 6 chunks
   chunks=[0]*6
   for i in range(6):
      # take rightmost 5 bits
      chunks[i]=value & 0x1F
      value=value>>5
   # pad by 0x20 for all but last chunk
   for i in range(5):
      chunks[i]^=0x20
   return chunks

def encode(value):
   '''
   Impelments encoding algorithm from:
   https://developers.google.com/maps/documentation/utilities/polylinealgorithm
   '''
   # 1. Take the initial signed value:
   # 2. Take the decimal value and multiply it by 1e5, rounding the result:
   value=int(round(float(value)*1e5))
   # 3. Convert the decimal value to binary. Note that a negative value must be calculated using its
   # two's complement by inverting the binary value and adding one to the result:
   value_bin=twos_comp_signed(value)
   # 4. Left-shift the binary value one bit:
   value_bin=value_bin<<1
   # 5. If the original decimal value is negative, invert this encoding:
   if value<0:
      value_bin=int(ctypes.c_uint32(~value_bin).value)
   # 6. Break the binary value out into 5-bit chunks (starting from the right hand side):
   # 7. Place the 5-bit chunks into reverse order:
   # 8. OR each value with 0x20 if another bit chunk follows:
   chunks=chunk_reverse_pad(value_bin)
   # 9. Convert each value to decimal:
   # 10. Add 63 to each value:
   # 11. Convert each value to its ASCII equivalent:
   characters=[chr(chunk+63) for chunk in chunks]
   return ''.join(characters)

def get_xml(zipcode):
   if len(zipcode) != 5:
      return None
   r = requests.get("http://maps.huge.info/zipv0.pl?ZIP={0}".format(zipcode))
   if r.status_code not in [200]:
      raise ZipCodeError("Unable to retrieve zip code data!")

   xmlroot=ET.fromstring(r.text)
   info_elem = xmlroot.find('info')
   if info_elem.attrib['count']=='0':
      raise ZipCodeError("Zip code not found!")

   return xmlroot

def generate_encoded_pathspecs(xmlroot):
   # basically, we're going to start at 50m between points, and keep increasing the distance
   # between points until we can fit the polyline in a URL
   # set it equal to 25m because it gets bumped up to 50m at beginning of firs tloop
   min_distance_meters=25
   min_distance_step=25

   # we'll stop trying different distances once we have an encoded string that will fit in a URL
   num_characters=sys.maxint
   # URL limit is 2048, and we need to account for the rest of the URL and encoding
   max_num_characters=1300

   while num_characters>max_num_characters:
      min_distance_meters+=min_distance_step

      prev_points={}
      points={}
      last_points={}
      init_points={}
      for elem in xmlroot:
         if elem.tag.startswith("polyline"):
            p=elem.tag
            curr_point=(float(elem.attrib['lat']),float(elem.attrib['lng']))
            if p not in points.keys():
               prev_points[p]=(None,None)
               points[p]=[]
               init_points[p]=curr_point
            if prev_points[p]==(None,None):
               points[p].extend(curr_point)
               prev_points[p]=curr_point
            elif great_circle(curr_point,prev_points[p]).meters > min_distance_meters:
               points[p].extend([curr_x-prev_x for curr_x,prev_x in zip(curr_point,prev_points[p])])
               prev_points[p]=curr_point
            last_points[p]=curr_point

      for p in points:
         # make sure last points each polyline are in the appropriate points list
         if prev_points[p] != last_points[p]:
            points[p].extend([curr_x-prev_x for curr_x,prev_x in \
               zip(last_points[p],prev_points[p])])

      merged_points={}
      do_merge={}
      for p in points:
         do_merge[p]=True
      for p1 in sorted(points.keys()):
         if do_merge[p1]:
            merged_points[p1]=[]
            merged_points[p1].extend(points[p1])
            for p2 in sorted(points.keys()):
               if p1 != p2:
                  if last_points[p1]==init_points[p2]:
                     # p2 is a continuation of p1, add the points form p1 to p2 and delete p2
                     merged_points[p1].extend(points[p2][2:])
                     last_points[p1]=last_points[p2]
                     do_merge[p2]=False

      encoded_pathspecs=[]
      num_characters=0
      for point_name in merged_points:
         encoded_points=[encode(point) for point in merged_points[point_name]]
         encoded_polyline="".join(encoded_points)
         idx=int(point_name[-1])
         encoded_pathspecs.append("weight:3|color:{0}|fillcolor:{1}|enc:{2}".format(\
            'blue','purple',encoded_polyline))

         num_characters+=len(encoded_pathspecs[-1])

   return encoded_pathspecs

def find_city_name(xmlroot):
   info_elem = xmlroot.find('info')
   return "{0}, {1}".format(info_elem.attrib['zipname'],info_elem.attrib['stname'])

def generate_image(xmlroot,filename):

   if not os.path.isfile(filename):
      encoded_pathspecs = generate_encoded_pathspecs(xmlroot)
      url='https://maps.googleapis.com/maps/api/staticmap'
      params={'size':'800x800','path':encoded_pathspecs}
      r = requests.get(url,params=params)

      if r.status_code not in [200]:
         raise ZipCodeError("Unable to retrieve map!")
      with open(filename, 'wb') as fd:
       for chunk in r.iter_content(1024):
           fd.write(chunk)

def generate_text_and_image(zipcode):
   xmlroot=get_xml(str(zipcode))
   filename="{0}.png".format(zipcode)
   generate_image(xmlroot,filename)
   message=find_city_name(xmlroot)
   return message,filename

class ZipCodeBot(TwitterBot):
   def on_mentions(self,statuses):
      for status in statuses:
         status_txt=self.strip_at_symbols(status.text).strip()
         if len(status_txt) != 5:
            self.reply(status,"Please use a 5-digit U.S.A. zip code!")
         else:
            try:
               message,filename=generate_text_and_image(status_txt)
               self.reply_with_image(status,filename,message)
            except ZipCodeError,zce:
               self.reply(status,"Error: {0}".format(zce.message))


if __name__ == "__main__":
   #zipcode="12345"
   #message,_=generate_text_and_image(zipcode)
   #print message
   bot = ZipCodeBot("zipcodebot.oauth")
   bot.run()
