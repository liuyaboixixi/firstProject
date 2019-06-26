package com.javaproject.demo.mapper;

import com.javaproject.demo.model.Review;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface ReviewMapper {
    @Insert("insert into review (id,parent_id,type ,commentator ,gmt_create ,gmt_modified ," +
            "like_count,content ) values (1,#{parentId},#{type},#{commentator},#{gmtCreate},#{gmtModified},#{likeCount},#{content})")
    void insert(Review comment);

}
